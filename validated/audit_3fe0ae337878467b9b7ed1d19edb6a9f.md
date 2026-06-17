### Title
No Maximum Fee Guard in Entropy `requestHelper` Allows Provider Front-Running to Extract User Overpayment — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `requestHelper` function in `Entropy.sol` does not refund excess `msg.value` to the caller, and any registered provider can change their fee at any time via `setProviderFee`. A malicious provider can front-run a user's `request`/`requestV2` transaction by raising their fee to match the user's `msg.value`, capturing the user's safety buffer as additional accrued provider fees. The user has no mechanism to specify a maximum acceptable fee, directly mirroring the "no minimum output" vulnerability class in the reference report.

---

### Finding Description

The `requestHelper` function in `Entropy.sol` handles fee accounting as follows: [1](#0-0) 

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

The interface documentation explicitly warns that fees can change and that excess is not refunded: [2](#0-1) 

> "Note that the fee can change over time... Further note that excess value is *not* refunded to the caller."

Because fees can change, users are implicitly encouraged to send a buffer above `getFeeV2()` to avoid reverts. Any registered provider can call `setProviderFee` at any time with no delay or timelock: [3](#0-2) 

**Attack path:**

1. User calls `getFeeV2(provider)` off-chain → returns `F_provider + F_pyth`.
2. User submits `request{value: F_provider + F_pyth + buffer}` to the mempool.
3. Provider monitors the mempool and front-runs by calling `setProviderFee(F_provider + buffer)`.
4. User's transaction executes. Now `requiredFee = F_provider + buffer + F_pyth = msg.value` — the check passes.
5. `providerInfo.accruedFeesInWei += F_provider + buffer` — provider captures the user's buffer.
6. `_state.accruedPythFeesInWei += F_pyth` — Pyth receives its normal share.
7. User paid `buffer` extra with no recourse and no refund.

The user has no parameter analogous to a `minSharesOut` or `maxFeeIn` to bound what they are willing to pay.

---

### Impact Explanation

Users lose native ETH (their safety buffer) to a provider who front-runs their request transaction. Because the excess `msg.value` is irrevocably split between the provider and Pyth with no refund path, the user cannot recover the overpayment. For high-value or high-frequency consumers (e.g., on-chain games, DeFi protocols using Entropy), the cumulative loss can be significant. The provider can repeat this attack on every request.

---

### Likelihood Explanation

Provider registration is permissionless — anyone can call `register` and become a provider. A malicious provider can set an attractive fee to attract users, then systematically front-run their requests. Even a legitimate provider (e.g., Fortuna) automatically adjusts fees via its keeper: [4](#0-3) 

This means fee changes are a normal, frequent on-chain event, making the race condition realistic even without malicious intent. On chains with a public mempool (most EVM chains), front-running is straightforward.

---

### Recommendation

1. **Add a `maxFee` parameter** to `request`, `requestWithCallback`, and `requestV2` variants. Revert if `requiredFee > maxFee`. This gives callers the same protection as a `minAmountOut` slippage guard.
2. **Alternatively, refund excess `msg.value`** to the caller after deducting `requiredFee`, so users can safely send a buffer without losing funds.

---

### Proof of Concept

```solidity
// 1. Provider registers with fee = 1000 wei
provider.register(1000, ...);

// 2. User queries fee: getFeeV2(provider) = 1000 + pythFee = 1100 wei
// User sends 1200 wei (100 wei buffer to be safe)

// 3. Provider sees the pending tx and front-runs:
provider.setProviderFee(1100); // raise fee by 100 (the user's buffer)

// 4. User's tx executes:
//    requiredFee = 1100 + 100 (pythFee) = 1200 = msg.value ✓ (no revert)
//    providerInfo.accruedFeesInWei += 1100  ← provider stole 100 wei buffer
//    _state.accruedPythFeesInWei += 100     ← pyth gets normal share
//    User: paid 1200, expected to pay 1100, lost 100 with no refund
``` [5](#0-4) [3](#0-2) [2](#0-1)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-820)
```text
    function setProviderFee(uint128 newFeeInWei) external override {
        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
        uint128 oldFeeInWei = provider.feeInWei;
        provider.feeInWei = newFeeInWei;
        emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** apps/fortuna/src/keeper/fee.rs (L381-382)
```rust
        let contract_call = contract.set_provider_fee_as_fee_manager(provider_address, target_fee);
        send_and_confirm(contract_call).await?;
```
