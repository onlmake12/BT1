### Title
Excess `msg.value` Beyond `requiredFee` Is Not Refunded to Caller in `requestPriceUpdatesWithCallback` — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, the `requestPriceUpdatesWithCallback` function accepts `msg.value`, computes a dynamic `requiredFee`, but stores the entire `msg.value - pythFee` as `req.fee` (the provider's payout). Any overpayment beyond `requiredFee` is silently transferred to the provider rather than refunded to the caller.

---

### Finding Description

`requestPriceUpdatesWithCallback` computes a dynamic fee via `getFee(provider, callbackGasLimit, priceIds)` and enforces a minimum:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

Immediately after, the stored provider fee is set to the full `msg.value - pythFee`, not `requiredFee - pythFee`:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

When `executeCallback` is later called, the provider is credited with `req.fee + msg.value(executeCallback) - pythFee`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

So if a caller sends `requiredFee + X`, the provider receives an extra `X` wei that belongs to the caller. There is no refund path.

The `getFee` function is composed of multiple dynamic components:

```solidity
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
``` [4](#0-3) 

Because the provider can update their fee at any time via `setProviderFee`, the fee is volatile. Callers who add a safety buffer (a common pattern) or whose transactions are delayed will overpay, with the excess silently captured by the provider.

**Contrast with Entropy**, which explicitly documents this behavior in its interface:

> "Note that excess value is *not* refunded to the caller." [5](#0-4) 

Echo has no such documentation and no analogous design intent.

---

### Impact Explanation

Any caller of `requestPriceUpdatesWithCallback` who sends `msg.value > getFee(provider, callbackGasLimit, priceIds)` permanently loses the excess ETH to the provider. The provider can immediately withdraw it via `withdrawAsFeeManager` or direct withdrawal. There is no mechanism for the caller to recover the overpayment.

---

### Likelihood Explanation

- The fee is dynamic and provider-controlled; it can change between the block a user queries `getFee()` and the block their transaction lands.
- Callers commonly add a small buffer to avoid `InsufficientFee` reverts, especially when gas prices fluctuate.
- Any integrating contract that hardcodes a fee estimate or adds a buffer will silently overpay on every call.

---

### Recommendation

After deducting `requiredFee` from `msg.value`, refund the remainder to `msg.sender`:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();

// Refund excess
uint256 excess = msg.value - requiredFee;
if (excess > 0) {
    (bool sent, ) = msg.sender.call{value: excess}("");
    require(sent, "Refund failed");
}

req.fee = SafeCast.toUint96(requiredFee - _state.pythFeeInWei);
_state.accruedFeesInWei += _state.pythFeeInWei;
```

---

### Proof of Concept

1. Provider registers with `baseFeeInWei = 0.001 ether`, `feePerFeedInWei = 0`, `feePerGasInWei = 0`.
2. `pythFeeInWei = 0.0001 ether`. So `getFee(provider, 0, [priceId]) = 0.0011 ether`.
3. Caller sends `msg.value = 0.002 ether` (adding a buffer).
4. `req.fee = 0.002 ether - 0.0001 ether = 0.0019 ether`.
5. Provider calls `executeCallback`, receives `0.0019 ether` instead of `0.001 ether`.
6. Provider withdraws the extra `0.0009 ether` that belonged to the caller. [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L250-254)
```text
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
