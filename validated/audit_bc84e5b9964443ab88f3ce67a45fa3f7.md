### Title
Excess ETH Sent to `requestPriceUpdatesWithCallback()` Is Not Refunded to the User — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `requestPriceUpdatesWithCallback()` is a `payable` function that accepts ETH fees from users. When a user sends more ETH than the required fee, the excess is silently absorbed as additional provider fee rather than being returned to the caller. This is a direct, permanent loss of user funds with no recovery path.

---

### Finding Description

`requestPriceUpdatesWithCallback()` computes a `requiredFee` and enforces a minimum via `if (msg.value < requiredFee) revert InsufficientFee()`. However, it never enforces an upper bound or issues a refund. The entire `msg.value` is split between Pyth and the provider:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
// ...
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);   // provider gets ALL excess
// ...
_state.accruedFeesInWei += _state.pythFeeInWei;                 // Pyth gets exactly pythFeeInWei
``` [1](#0-0) 

If `msg.value = requiredFee + X` (where X > 0), then:
- Pyth accrues exactly `_state.pythFeeInWei` (correct).
- The provider's stored `req.fee` becomes `msg.value - _state.pythFeeInWei = providerFee + X` — the provider receives the full overpayment.
- The user receives nothing back.

There is no `payable(msg.sender).transfer(excess)` or equivalent refund anywhere in the function. [2](#0-1) 

This contrasts with `PythLazer.sol`'s `verifyUpdate()`, which explicitly handles the refund case:

```solidity
if (msg.value > verification_fee) {
    payable(msg.sender).transfer(msg.value - verification_fee);
}
``` [3](#0-2) 

The analogous Entropy `requestHelper` has the same pattern — all of `msg.value` beyond `providerFee` is credited to `accruedPythFeesInWei` with no refund — and the `IEntropyV2` interface even explicitly documents this as known behavior ("excess value is *not* refunded to the caller"). [4](#0-3) [5](#0-4) 

Echo.sol carries no such documentation, making the missing refund an unintentional design gap.

---

### Impact Explanation

Any ETH sent above `requiredFee` is permanently transferred to the provider's `req.fee` balance and later paid out to the provider on `executeCallback`. The user has no mechanism to recover the excess. This is a direct, irreversible loss of user funds proportional to the overpayment amount.

---

### Likelihood Explanation

Overpayment is a realistic scenario because:
1. Provider fees can change between the time a user queries `getFee()` and the time the transaction is mined (providers can call `setProviderFee()`).
2. Users or integrating contracts may add a buffer to `msg.value` to avoid `InsufficientFee` reverts.
3. Front-end or SDK estimation errors can produce a higher-than-needed value.

---

### Recommendation

After computing `requiredFee`, refund any excess to `msg.sender`:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();

uint256 excess = msg.value - requiredFee;
if (excess > 0) {
    (bool ok, ) = payable(msg.sender).call{value: excess}("");
    require(ok, "Refund failed");
}
```

The same fix should be applied to `Entropy.sol`'s `requestHelper` and its documented "no refund" behavior should be reconsidered.

---

### Proof of Concept

1. Provider registers with `baseFeeInWei = 1 ether`.
2. User calls `requestPriceUpdatesWithCallback{value: 2 ether}(provider, ...)` (overpays by 1 ether).
3. `requiredFee` = 1 ether + `pythFeeInWei`. `msg.value` passes the `< requiredFee` check.
4. `req.fee = SafeCast.toUint96(2 ether - pythFeeInWei)` — provider's stored fee includes the 1 ether excess.
5. Provider calls `executeCallback(...)`, receives `req.fee` which includes the user's 1 ether overpayment.
6. User's 1 ether is permanently lost with no revert or refund. [6](#0-5)

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

**File:** lazer/contracts/evm/src/PythLazer.sol (L74-77)
```text
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-239)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L94-96)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
