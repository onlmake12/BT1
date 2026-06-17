### Title
Entropy Fee Accounting Incorrectly Attributes All Overpayment to Pyth Protocol — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

In `Entropy.sol`'s `requestHelper`, the Pyth protocol fee bucket is credited with `msg.value - providerFee` instead of the fixed `_state.pythFeeInWei`. Any overpayment beyond the required fee is silently captured by the Pyth protocol rather than being refunded to the caller or correctly split. This is the direct analog of the EIP-2981 issue: a constant (the configured `pythFeeInWei`) should be used for the Pyth cut, but instead a derived value (`msg.value - providerFee`) is used, causing the Pyth fee to be inflated whenever a user overpays.

### Finding Description

In `requestHelper` (called by every `request`, `requestWithCallback`, and `requestV2` entry point):

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);   // = providerFee + pythFeeInWei
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee); // BUG
``` [1](#0-0) 

The intended split is:
- Provider receives: `providerFee`
- Pyth receives: `pythFeeInWei`
- User refund: `msg.value - providerFee - pythFeeInWei`

The actual split is:
- Provider receives: `providerFee`
- Pyth receives: `msg.value - providerFee` (= `pythFeeInWei + excess`)
- User refund: **0** — all excess is captured by Pyth

The NatDoc for `requestWithCallback` and `request` explicitly acknowledges this: *"Note that excess value is not refunded to the caller."* [2](#0-1) 

The `getFeeV2` function confirms the intended fee structure is `providerFee + pythFeeInWei`: [3](#0-2) 

### Impact Explanation

Any user who sends `msg.value > requiredFee` (a common pattern to avoid reverts from gas price fluctuations or fee changes between estimation and submission) permanently loses the excess ETH to the Pyth protocol fee bucket (`accruedPythFeesInWei`). The provider receives only their correct fee; the excess does not benefit the provider either. The Pyth protocol fee is effectively scaled up beyond its governance-configured value (`pythFeeInWei`) on every overpaying request. This constitutes a direct, unrecoverable financial loss for users.

### Likelihood Explanation

This is triggered by any unprivileged user calling `request`, `requestWithCallback`, or `requestV2` with `msg.value > getFeeV2(provider, gasLimit)`. This is a routine occurrence: wallets and integrations routinely add a buffer to avoid `InsufficientFee` reverts when fees change between block estimation and submission. The Entropy documentation itself warns fees are dynamic. Every such overpaying transaction silently donates the excess to the Pyth fee pool. [4](#0-3) 

### Recommendation

Replace the residual-based Pyth fee credit with the exact configured fee, and refund any excess to the caller:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += _state.pythFeeInWei; // honor exact configured fee

// Refund excess to caller
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool sent, ) = msg.sender.call{value: excess}("");
    require(sent, "refund failed");
}
```

### Proof of Concept

1. Pyth governance sets `pythFeeInWei = 1000 wei`. Provider sets `feeInWei = 5000 wei`. `getFeeV2 = 6000 wei`.
2. User calls `requestWithCallback{value: 10000}(provider, userRandomness)` — overpaying by 4000 wei.
3. `providerInfo.accruedFeesInWei += 5000` ✓
4. `_state.accruedPythFeesInWei += (10000 - 5000) = 5000` ✗ — should be 1000.
5. The 4000 wei excess is permanently captured by the Pyth protocol fee bucket, not refunded to the user and not credited to the provider. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-239)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L320-322)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function request(
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L760-765)
```text
    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
    }
```
