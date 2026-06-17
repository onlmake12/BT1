### Title
Entropy `requestHelper` Silently Captures All User Overpayment as Pyth Protocol Fees With No Refund Path — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `requestHelper` function credits the entire `msg.value - providerFee` to `_state.accruedPythFeesInWei`. Any amount sent by the user in excess of the exact required fee (`providerFee + pythFeeInWei`) is permanently captured by the Pyth protocol treasury. There is no refund mechanism and no way for the user to recover the excess. This is an inaccurate user-protection guarantee: the protocol accepts overpayments silently and routes them to an admin-controlled treasury.

---

### Finding Description

In `requestHelper` at lines 233–239:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

`getFeeV2(provider, callbackGasLimit)` returns `getProviderFee(provider, callbackGasLimit) + _state.pythFeeInWei`. [2](#0-1) 

When `msg.value > requiredFee`, the accounting becomes:

- `providerInfo.accruedFeesInWei += providerFee` (correct)
- `_state.accruedPythFeesInWei += pythFeeInWei + excess` (excess silently captured)

The excess is never returned to `msg.sender`. The only way to recover funds from `accruedPythFeesInWei` is via `EntropyGovernance.withdrawFee()`, which is restricted to the admin/owner:

```solidity
function withdrawFee(address targetAddress, uint128 amount) external {
    require(targetAddress != address(0), "targetAddress is zero address");
    _authoriseAdminAction();
    ...
    _state.accruedPythFeesInWei -= amount;
    (bool success, ) = targetAddress.call{value: amount}("");
``` [3](#0-2) 

The user has no path to recover their overpayment.

---

### Impact Explanation

Any user who sends `msg.value > getFeeV2(provider, gasLimit)` permanently loses the excess to the Pyth treasury. Concrete loss scenarios:

1. **Buffer overpayment**: Integrating contracts commonly send a small buffer (e.g., `fee * 110 / 100`) to guard against fee changes between fee-query and transaction mining. The entire buffer is captured.
2. **Fee-increase race**: A provider calls `setProviderFee(newHigherFee)` between a user's `getFeeV2()` call and their `requestWithCallback` transaction. If the user sent a buffer large enough to cover the new fee, the transaction succeeds but the user loses `msg.value - new_requiredFee` to Pyth fees.
3. **Rounding overpayment**: `getProviderFee` rounds `gasLimit` up to 10k increments. A user who pays `getFeeV2(provider, 15000)` pays for 20000 gas worth of fee. If they sent any extra beyond that rounded amount, it is captured. [4](#0-3) 

The captured funds are only withdrawable by the admin, not the user.

---

### Likelihood Explanation

**Moderate.** The `IEntropy` and `IEntropyV2` interfaces document "excess value is *not* refunded to the caller": [5](#0-4) 

However, this documentation does not prevent the loss — it merely discloses it. In practice:

- Smart contract integrators routinely send a buffer to avoid reverts on fee changes.
- The Fortuna keeper itself dynamically adjusts provider fees via `setProviderFeeAsFeeManager`, creating windows where users' buffered payments are silently captured. [6](#0-5) 

The test suite confirms overpayment is silently absorbed into `accruedPythFees`: [7](#0-6) 

---

### Recommendation

Refund excess `msg.value` to the caller after crediting the exact required fee:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += _state.pythFeeInWei;
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool sent, ) = msg.sender.call{value: excess}("");
    require(sent, "refund failed");
}
```

---

### Proof of Concept

1. Provider registers with `feeInWei = 900 wei`. Admin sets `pythFeeInWei = 100 wei`.
2. User calls `getFeeV2(provider, 0)` → returns `1000 wei`.
3. User submits `requestWithCallback{value: 1100 wei}(provider, userRandomNumber)` (100 wei buffer).
4. Transaction succeeds.
5. State after:
   - `providerInfo.accruedFeesInWei += 900` ✓
   - `_state.accruedPythFeesInWei += 1100 - 900 = 200` (should be 100; user loses 100 wei)
6. User has no function to recover the 100 wei. Only the admin can call `withdrawFee()` to extract it. [8](#0-7) [9](#0-8)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L760-765)
```text
    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L780-793)
```text
        uint32 roundedGasLimit = uint32(roundTo10kGas(gasLimit)) * TEN_THOUSAND;
        if (
            provider.defaultGasLimit > 0 &&
            roundedGasLimit > provider.defaultGasLimit
        ) {
            // This calculation rounds down the fee, which means that users can get some gas in the callback for free.
            // However, the value of the free gas is < 1 wei, which is insignificant.
            uint128 additionalFee = ((roundedGasLimit -
                provider.defaultGasLimit) * provider.feeInWei) /
                provider.defaultGasLimit;
            return provider.feeInWei + additionalFee;
        } else {
            return provider.feeInWei;
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L67-74)
```text
    function setPythFee(uint128 newPythFee) external {
        _authoriseAdminAction();

        uint oldPythFee = _state.pythFeeInWei;
        _state.pythFeeInWei = newPythFee;

        emit PythFeeSet(oldPythFee, newPythFee);
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyGovernance.sol (L103-116)
```text
    function withdrawFee(address targetAddress, uint128 amount) external {
        require(targetAddress != address(0), "targetAddress is zero address");
        _authoriseAdminAction();

        if (amount > _state.accruedPythFeesInWei)
            revert EntropyErrors.InsufficientFee();

        _state.accruedPythFeesInWei -= amount;

        (bool success, ) = targetAddress.call{value: amount}("");
        require(success, "Failed to withdraw fees");

        emit FeeWithdrawn(targetAddress, amount);
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** apps/fortuna/src/keeper/fee.rs (L370-382)
```rust
    if is_chain_active
        && ((provider_fee > target_fee_max && can_reduce_fees) || provider_fee < target_fee_min)
    {
        if min_fee_wei * 100 < target_fee {
            return Err(anyhow!("Cowardly refusing to set target fee more than 100x min_fee_wei. Target: {:?} Min: {:?}", target_fee, min_fee_wei));
        }
        tracing::info!(
            "Adjusting fees. Current: {:?} Target: {:?}",
            provider_fee,
            target_fee
        );
        let contract_call = contract.set_provider_fee_as_fee_manager(provider_address, target_fee);
        send_and_confirm(contract_call).await?;
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L682-699)
```text
        // this call overpays for the random number
        requestWithFee(
            user2,
            pythFeeInWei + provider2FeeInWei + 10000,
            provider2,
            42,
            false
        );

        assertEq(
            random.getProviderInfoV2(provider1).accruedFeesInWei,
            provider1FeeInWei * 3
        );
        assertEq(
            random.getProviderInfoV2(provider2).accruedFeesInWei,
            provider2FeeInWei * 2
        );
        assertEq(random.getAccruedPythFees(), pythFeeInWei * 5 + 10000);
```
