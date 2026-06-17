### Title
Excess ETH Sent to `requestPriceUpdatesWithCallback` Is Not Refunded to Caller — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, the `requestPriceUpdatesWithCallback` function accepts any `msg.value >= requiredFee` but stores the entire `msg.value - pythFeeInWei` as the provider's fee (`req.fee`). Any ETH sent above the required fee is silently transferred to the provider when `executeCallback` is called, rather than being refunded to the caller. There is no refund step for the excess.

---

### Finding Description

In `requestPriceUpdatesWithCallback`, the contract computes the exact required fee:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

It then stores the provider's fee as:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

The correct provider fee should be `requiredFee - pythFeeInWei`. Instead, the contract stores `msg.value - pythFeeInWei`. When `msg.value > requiredFee`, the excess `(msg.value - requiredFee)` is silently added to `req.fee` and later credited entirely to the provider in `executeCallback`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

There is no `payable(msg.sender).transfer(msg.value - requiredFee)` or equivalent refund step anywhere in the function. [4](#0-3) 

By contrast, `PythLazer.sol`'s `verifyUpdate` correctly refunds excess:

```solidity
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [5](#0-4) 

The Entropy contract explicitly documents this as intentional behavior ("Note that excess value is *not* refunded to the caller"), but the Echo contract has no such documentation and the design intent appears to be that users pay exactly `getFee(...)`. [6](#0-5) 

---

### Impact Explanation

Any user who sends `msg.value > getFee(provider, callbackGasLimit, priceIds)` permanently loses the excess ETH to the provider. This is a direct, unrecoverable financial loss. The excess is not stuck in the contract — it is actively transferred to the provider's accrued balance, making it unrecoverable by the user.

---

### Likelihood Explanation

This is triggered by any unprivileged user calling `requestPriceUpdatesWithCallback`. It is realistic because:

1. Fees can change between the time a user queries `getFee()` and the time their transaction is mined (e.g., provider updates their fee via `registerProvider`).
2. Users or front-ends commonly send a small buffer above the estimated fee to avoid reverts.
3. Any contract integrating Echo that hardcodes a fee estimate or adds a safety margin will silently overpay the provider on every call.

---

### Recommendation

Refund the excess ETH to the caller after storing the correct provider fee:

```diff
  uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
  if (msg.value < requiredFee) revert InsufficientFee();

  req.fee = SafeCast.toUint96(requiredFee - _state.pythFeeInWei);
  _state.accruedFeesInWei += _state.pythFeeInWei;

+ if (msg.value > requiredFee) {
+     (bool refunded, ) = msg.sender.call{value: msg.value - requiredFee}("");
+     require(refunded, "Refund failed");
+ }
``` [7](#0-6) 

---

### Proof of Concept

1. Provider registers with `baseFeeInWei = 1000 wei`, `feePerFeedInWei = 100 wei`, `feePerGasInWei = 1 wei`. `pythFeeInWei = 500 wei`.
2. User calls `getFee(provider, 100000, [priceId])` → returns `500 + 1000 + 100 + 100000 = 101600 wei`.
3. User sends `msg.value = 102000 wei` (400 wei buffer to avoid revert risk).
4. Contract stores `req.fee = 102000 - 500 = 101500 wei` instead of the correct `101100 wei`.
5. When `executeCallback` is called, provider receives `101500 wei` instead of `101100 wei`.
6. User permanently loses `400 wei` with no recourse. [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;

        emit PriceUpdateRequested(req, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L74-77)
```text
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L18-19)
```text
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
