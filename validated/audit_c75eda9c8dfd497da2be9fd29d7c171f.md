### Title
Entropy Provider Can Frontrun `requestV2` Transactions to Extract Arbitrary Fees - (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

A registered Entropy provider can frontrun pending `requestV2` / `request` / `requestWithCallback` transactions in the mempool by atomically raising their `feeInWei` to match the user's `msg.value`, capturing the entire user payment minus the fixed Pyth protocol fee, then resetting the fee. Because excess `msg.value` is explicitly **not refunded** and there is no upper bound or timelock on `setProviderFee`, the provider can extract the full buffer a user sends.

### Finding Description

`setProviderFee` accepts any `uint128` value with no cap, no timelock, and no delay: [1](#0-0) 

Inside `requestHelper`, the fee is read from live storage at execution time. The only guard is `msg.value >= requiredFee`; there is no maximum-fee slippage parameter: [2](#0-1) 

The fee split is: provider receives `providerFee`, Pyth receives `msg.value - providerFee`. If the provider sets `feeInWei = msg.value - pythFeeInWei`, the check `msg.value >= requiredFee` still passes (equality), and the provider captures `msg.value - pythFeeInWei` — the entire user payment minus the fixed protocol fee.

The interface documentation explicitly warns that excess value is **not** refunded, which means users who add a buffer to ensure their transaction lands are the most exposed: [3](#0-2) 

### Impact Explanation

A malicious provider can drain the full `msg.value` (minus `pythFeeInWei`) from any user request. For integrating protocols that pass fees through to end-users, this is a direct, unrecoverable financial loss. The provider can also set `feeInWei = type(uint128).max` to cause all in-flight user requests to revert (DoS), then reset the fee — selectively griefing users at zero cost.

### Likelihood Explanation

Any permissionlessly registered provider can execute this attack. The attack requires only:
1. Watching the public mempool for `requestV2` calls targeting their provider address (the `provider` argument is visible in calldata).
2. Submitting a `setProviderFee` transaction with a higher gas price to land before the user's transaction.
3. Resetting the fee afterward.

On chains with transparent mempools (Ethereum mainnet, most L2s), this is straightforward. The Fortuna keeper itself already monitors and adjusts fees dynamically: [4](#0-3) 

This demonstrates that fee changes are a normal, automated operation — a malicious provider can do the same thing adversarially.

### Recommendation

1. **Add a maximum fee cap** enforced on-chain in `setProviderFee` (e.g., a governance-controlled `maxProviderFeeInWei`).
2. **Add a user-supplied `maxFee` slippage parameter** to `requestV2` that causes the transaction to revert if `getFeeV2(provider, gasLimit) > maxFee` at execution time.
3. **Refund excess `msg.value`** to the caller, so users do not need to send buffers that can be captured.
4. **Introduce a fee-change timelock** (e.g., a minimum delay between fee increases) to prevent atomic frontrunning.

### Proof of Concept

```solidity
// Attacker is a registered provider watching the mempool.
// User submits: requestV2{value: 0.01 ether}(attackerProvider, userRand, 100_000)
// Attacker frontruns:

// Step 1: raise fee to capture full msg.value
uint128 pythFee = entropy.getPythFee();           // e.g. 0.001 ether
entropy.setProviderFee(0.01 ether - pythFee);     // feeInWei = 0.009 ether

// Step 2: user's tx executes:
//   requiredFee = 0.009 + 0.001 = 0.01 ether == msg.value  ✓ passes
//   providerInfo.accruedFeesInWei += 0.009 ether  (attacker captures)
//   accruedPythFeesInWei += 0.001 ether

// Step 3: reset fee
entropy.setProviderFee(normalFee);

// Attacker withdraws
entropy.withdraw(0.009 ether);
```

The user pays `0.01 ether` but receives service worth only `normalFee` (e.g., `0.0001 ether`). The provider extracts `0.009 ether` in excess fees with no on-chain mechanism to prevent or detect it before the fact. [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L809-827)
```text
    // Set provider fee. It will revert if provider is not registered.
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
        emit EntropyEventsV2.ProviderFeeUpdated(
            msg.sender,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L94-96)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** apps/fortuna/src/keeper/fee.rs (L221-262)
```rust
#[tracing::instrument(name = "adjust_fee", skip_all)]
#[allow(clippy::too_many_arguments)]
pub async fn adjust_fee_wrapper(
    contract: Arc<InstrumentedSignablePythContract>,
    chain_state: BlockchainState,
    provider_address: Address,
    poll_interval: Duration,
    legacy_tx: bool,
    min_profit_pct: u64,
    target_profit_pct: u64,
    max_profit_pct: u64,
    min_fee_wei: u128,
    max_fee_wei: Option<u128>,
    metrics: Arc<KeeperMetrics>,
) {
    // The maximum balance of accrued fees + provider wallet balance. None if we haven't observed a value yet.
    let mut high_water_pnl: Option<U256> = None;
    // The sequence number where the keeper last updated the on-chain fee. None if we haven't observed it yet.
    let mut sequence_number_of_last_fee_update: Option<u64> = None;
    loop {
        if let Err(e) = adjust_fee_if_necessary(
            contract.clone(),
            chain_state.id.clone(),
            provider_address,
            legacy_tx,
            min_profit_pct,
            target_profit_pct,
            max_profit_pct,
            min_fee_wei,
            max_fee_wei,
            &mut high_water_pnl,
            &mut sequence_number_of_last_fee_update,
            metrics.clone(),
        )
        .in_current_span()
        .await
        {
            tracing::error!("Fee adjustment failed: {:?}", e);
        }
        time::sleep(poll_interval).await;
    }
}
```
