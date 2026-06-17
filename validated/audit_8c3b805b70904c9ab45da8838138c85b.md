### Title
Excess ETH Overpayment Silently Credited to Provider Instead of Refunded to Requester — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol#requestPriceUpdatesWithCallback()` accepts `msg.value >= requiredFee` but stores the **entire** `msg.value - pythFeeInWei` as `req.fee`, which is later fully credited to the provider in `executeCallback`. Any ETH overpayment beyond the required fee is permanently transferred to the provider rather than refunded to the original payer (`msg.sender`).

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` computes a `requiredFee` and enforces only a minimum check:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

It then stores the **full** `msg.value - pythFeeInWei` as the provider fee:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

Later, in `executeCallback`, the entire stored `req.fee` is credited to the provider:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [3](#0-2) 

There is **no refund path** for the excess. The correct behavior would be:

```
req.fee = requiredFee - pythFeeInWei   // only the required provider portion
refund(msg.sender, msg.value - requiredFee)  // return the excess
```

The same pattern exists in `Entropy.sol#requestHelper`, where excess `msg.value` beyond `providerFee` is silently added to `accruedPythFeesInWei` (Pyth treasury) rather than refunded:

```solidity
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [4](#0-3) 

This is explicitly documented as intentional in the interface NatDoc — "excess value is *not* refunded to the caller" — but the Echo contract has no such disclaimer and the behavior is a direct loss to the user. [5](#0-4) 

---

### Impact Explanation

**Direct loss of funds for the requester.** If a user (or an integrating contract) sends `msg.value` greater than `requiredFee` — for example, to guard against fee fluctuations or to match a pre-computed upper bound — the entire excess is silently credited to the provider's `accruedFeesInWei` balance. The user has no mechanism to recover this ETH. The provider can then withdraw it via `withdrawAsFeeManager`.

Concrete example:
- `requiredFee = 900 wei`, `pythFeeInWei = 100 wei`
- User sends `msg.value = 1000 wei` (100 wei buffer)
- `req.fee = 1000 - 100 = 900 wei` (should be `800 wei`)
- Provider receives `900 wei` instead of `800 wei`; user loses `100 wei` permanently [6](#0-5) 

---

### Likelihood Explanation

**Medium.** The Echo fee is dynamic — it includes a `feePerGasInWei * callbackGasLimit` component that fluctuates with gas price. Integrators commonly send a small ETH buffer above the computed fee to avoid reverts due to fee increases between fee-query and transaction submission. Any such buffer is permanently lost. The entry path requires only an unprivileged call to `requestPriceUpdatesWithCallback` with `msg.value > requiredFee`. [7](#0-6) 

---

### Recommendation

1. Compute the exact required provider fee and store only that amount:
   ```solidity
   uint96 providerFee = requiredFee - _state.pythFeeInWei;
   req.fee = providerFee;
   ```
2. Refund any excess to `msg.sender`:
   ```solidity
   if (msg.value > requiredFee) {
       (bool ok,) = msg.sender.call{value: msg.value - requiredFee}("");
       require(ok, "refund failed");
   }
   ```

---

### Proof of Concept

1. Deploy Echo with `pythFeeInWei = 100 wei`, provider registered with `baseFeeInWei = 800 wei`, `feePerGasInWei = 0`, `feePerFeedInWei = 0`.
2. `requiredFee = getFee(provider, 0, priceIds) = 900 wei`.
3. Call `requestPriceUpdatesWithCallback{value: 1000}(...)` — 100 wei buffer.
4. Observe `req.fee = 1000 - 100 = 900` (not `800`).
5. After `executeCallback`, provider's `accruedFeesInWei` increases by `900 wei` instead of `800 wei`.
6. Provider calls `withdrawAsFeeManager` and withdraws the extra `100 wei` that belonged to the user.
7. User has no recourse. [2](#0-1) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L238-239)
```text
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L18-19)
```text
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
