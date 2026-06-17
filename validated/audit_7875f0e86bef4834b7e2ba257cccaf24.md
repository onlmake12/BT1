### Title
Excess `msg.value` in `Entropy.requestHelper()` Is Silently Absorbed Into Pyth Treasury Instead of Being Refunded — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.requestHelper()` accepts any `msg.value >= requiredFee` but credits the entire surplus above `providerFee` to `_state.accruedPythFeesInWei` (Pyth's treasury) without refunding the caller. Any unprivileged user who overpays when calling `requestV2()` or `request()` permanently loses the excess to Pyth's fee pool.

---

### Finding Description

`requestHelper` is the internal function backing all public entropy request entry points (`requestV2()`, `request()`, `requestWithCallback()`). Its fee accounting logic is:

```solidity
// Entropy.sol lines 234–239
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

The check uses strict `<` (i.e., `>=` is accepted). When `msg.value = requiredFee + X`:

- Provider receives exactly `providerFee` ✓
- Pyth receives `msg.value - providerFee = pythFee + X` — absorbing the overpayment `X`
- Caller receives nothing back

The code even documents this behavior at line 321:

> "Note that excess value is *not* refunded to the caller."

`accruedPythFeesInWei` is withdrawable only by Pyth governance (`EntropyGovernance.sol`), so the user's excess is permanently transferred to Pyth's treasury with no user-facing recovery path.

---

### Impact Explanation

Any user who overpays — due to a frontend estimation rounding up, a wallet adding a buffer, or a direct contract call — loses the excess ETH permanently. The funds are not locked in the contract; they are credited to Pyth's treasury and can be withdrawn by Pyth governance. This constitutes a direct, unrecoverable financial loss for the caller with no on-chain remedy.

---

### Likelihood Explanation

Entropy fee amounts depend on `tx.gasprice` (for callback gas cost) and provider-set fees, both of which can change between fee estimation and transaction inclusion. Wallets and integrators commonly add a small buffer to `msg.value` to avoid reverts. Every such overpayment silently enriches Pyth's treasury at the user's expense. The entry path requires no privilege — any EOA or contract can call `requestV2()`.

---

### Recommendation

Refund excess `msg.value` to the caller after crediting the exact required fee, analogous to the pattern already used in `PythLazer.sol` lines 75–77:

```solidity
if (msg.value > requiredFee) {
    payable(msg.sender).transfer(msg.value - requiredFee);
}
```

Alternatively, enforce strict equality: `if (msg.value != requiredFee) revert`.

---

### Proof of Concept

1. Deploy Entropy on a testnet with a provider whose `feeInWei = 1000 wei`.
2. Call `getFeeV2(provider, 0)` → returns `requiredFee` (e.g., `1500 wei`).
3. Call `requestV2{value: 2000}()` (overpaying by `500 wei`).
4. Observe: `_state.accruedPythFeesInWei` increases by `2000 - providerFee` (absorbing the 500 wei surplus). Caller's balance is reduced by the full 2000 wei. No refund is issued. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L319-322)
```text
    //
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function request(
```

**File:** lazer/contracts/evm/src/PythLazer.sol (L73-77)
```text
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
