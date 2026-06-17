### Title
Excess ETH Paid by User in `requestPriceUpdatesWithCallback` Is Not Refunded and Silently Credited to Provider — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.requestPriceUpdatesWithCallback` validates only that `msg.value >= requiredFee` but never refunds the excess. Any ETH above `requiredFee` is silently stored in `req.fee` and later credited in full to the provider's accrued balance via `executeCallback`. The user permanently loses the overpayment.

---

### Finding Description

In `Echo.sol`, `requestPriceUpdatesWithCallback` performs a minimum-fee check:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [1](#0-0) 

It then stores the entire amount above `pythFeeInWei` — including any user overpayment — as the request's fee:

```solidity
req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
``` [2](#0-1) 

Only the fixed `pythFeeInWei` is credited to Pyth:

```solidity
_state.accruedFeesInWei += _state.pythFeeInWei;
``` [3](#0-2) 

No refund is issued. When `executeCallback` is later called, the provider receives the full `req.fee`:

```solidity
_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((req.fee + msg.value) - pythFee);
``` [4](#0-3) 

This means any ETH the user sent above `requiredFee` is permanently transferred to the provider's withdrawable balance. The `IEcho` interface NatSpec even states `"The msg.value must be equal to getFee(callbackGasLimit)"`, implying exact payment — yet the implementation silently accepts and absorbs any surplus without warning or refund. [5](#0-4) 

---

### Impact Explanation

Any ETH overpaid by a user calling `requestPriceUpdatesWithCallback` is permanently lost to that user and unilaterally credited to the provider. For example, if `requiredFee` is 0.01 ETH and the user sends 0.05 ETH (a common defensive pattern to avoid reverts due to fee changes), the provider receives an extra 0.04 ETH the user never intended to pay. There is no mechanism for the user to recover this amount.

---

### Likelihood Explanation

This is realistically triggered because:

1. **Fee volatility:** Providers can update their fees at any time via `setProviderFee`. A user who calls `getFee` to estimate and then submits the transaction in a later block may find the fee has changed, causing them to send a buffer.
2. **Defensive overpayment:** Integrators and wallets routinely add a margin to `msg.value` to avoid reverts, a standard practice in DeFi.
3. **Misleading interface:** The `IEcho` NatSpec says `msg.value` "must be equal to" the fee, giving no indication that excess is non-refundable and will be given to the provider. Contrast this with `IEntropyV2`, which explicitly documents: *"excess value is not refunded to the caller"* — Echo has no such disclosure. [6](#0-5) 

---

### Recommendation

Add a refund of excess ETH to the caller at the end of `requestPriceUpdatesWithCallback`:

```solidity
uint256 excess = msg.value - requiredFee;
if (excess > 0) {
    (bool refunded, ) = msg.sender.call{value: excess}("");
    require(refunded, "Refund failed");
}
```

Alternatively, if absorbing excess is intentional, update the `IEcho` NatSpec to explicitly state that excess ETH is non-refundable and will be credited to the provider, matching the disclosure pattern used in `IEntropyV2`.

---

### Proof of Concept

1. Provider registers with `baseFeeInWei = 0.001 ETH`, `feePerFeedInWei = 0`, `feePerGasInWei = 0`. Pyth fee is `0.001 ETH`. `requiredFee = 0.002 ETH`.
2. User calls `requestPriceUpdatesWithCallback{value: 0.01 ETH}(...)` — sending 5× the required fee as a buffer.
3. `req.fee` is set to `0.01 ETH - 0.001 ETH = 0.009 ETH`. Pyth accrues `0.001 ETH`. No refund is issued.
4. Provider calls `executeCallback`. Provider's `accruedFeesInWei` increases by `req.fee - pythFee_at_callback = 0.009 ETH - (actual Pyth update fee)`.
5. The user's `0.008 ETH` overpayment (above `requiredFee`) is permanently in the provider's balance. The user has no recourse. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-101)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-162)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L41-41)
```text
     * @dev The msg.value must be equal to getFee(callbackGasLimit)
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L94-96)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
