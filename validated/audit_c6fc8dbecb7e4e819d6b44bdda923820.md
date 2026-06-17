### Title
Excess `msg.value` Sent to Entropy Requests Is Permanently Captured as Protocol Fees, Not Refunded to Callers — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In the Entropy contract's `requestHelper` function, any ETH sent in excess of the required fee is silently and permanently captured as `accruedPythFeesInWei` rather than being refunded to the caller. Every public entry point — `request`, `requestWithCallback`, `requestV2` — shares this path. A caller who overpays by any amount loses that ETH irrecoverably to the Pyth protocol treasury.

---

### Finding Description

`requestHelper` performs the following fee accounting:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The expression `msg.value - providerFee` is assigned entirely to `accruedPythFeesInWei`. When `msg.value` equals exactly `requiredFee`, this equals `pythFee` — correct. But when `msg.value > requiredFee`, the surplus is silently absorbed into the Pyth protocol balance. There is no code path that returns excess ETH to `msg.sender`.

This is confirmed by the interface documentation, which explicitly states the behavior across every variant:

> "Note that excess value is *not* refunded to the caller." [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) 

The documentation treats this as intentional design, but the consequence is structurally identical to the external report: a caller is required to commit more value to the contract than the protocol actually needs to service the request, and the surplus is never returned.

---

### Impact Explanation

Any caller who sends `msg.value > getFeeV2(provider, gasLimit)` permanently loses the difference. The excess is credited to `_state.accruedPythFeesInWei`, which is only withdrawable by the Pyth admin/governance — not by the original caller. [7](#0-6) 

This is a direct, irreversible loss of native token funds for the requesting user. The captured surplus accrues to the protocol treasury with no mechanism for the original sender to reclaim it.

---

### Likelihood Explanation

The fee is explicitly dynamic. The `IEntropyV2` documentation warns callers to re-query `getFeeV2()` before every invocation because "the fee can change over time." [8](#0-7) 

In practice:
- Integrating contracts that cache the fee value between calls will overpay when the fee increases.
- Users who add a small ETH buffer to avoid `InsufficientFee` reverts lose that buffer permanently.
- Any front-end that estimates the fee slightly before submission (common in high-latency environments) will produce overpayments.

All three scenarios are realistic and routine for any production Entropy consumer.

---

### Recommendation

After crediting the exact `requiredFee` to the provider and Pyth balances, refund any surplus to `msg.sender`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();

uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (requiredFee - providerFee); // use requiredFee, not msg.value

// Refund excess
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool sent, ) = msg.sender.call{value: excess}("");
    require(sent, "refund failed");
}
```

This mirrors the fix applied in the referenced external report (PR #152): remove the unnecessary value capture so callers are not penalised for sending more than the minimum.

---

### Proof of Concept

1. Provider fee = 80 wei; Pyth fee = 20 wei; `requiredFee` = 100 wei.
2. Caller queries `getFeeV2()` → 100 wei, then sends `msg.value = 150 wei` as a safety buffer.
3. `requestHelper` executes:
   - `providerInfo.accruedFeesInWei += 80`
   - `_state.accruedPythFeesInWei += (150 − 80) = 70` ← 50 wei surplus absorbed
4. Caller receives no refund. The 50 wei surplus is permanently locked in the Pyth treasury.
5. The only way to recover those funds is via a privileged `withdrawFee` call by the Pyth admin — the original caller has no recourse. [1](#0-0)

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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L46-47)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L66-67)
```text
    // This method will revert unless the caller provides a sufficient fee (at least `getFee(provider)`) as msg.value.
    // Note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L42-44)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(gasLimit)`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2(gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L66-69)
```text
    ///
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L94-96)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```
