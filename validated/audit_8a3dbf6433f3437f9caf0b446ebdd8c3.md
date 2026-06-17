### Title
Entropy Provider Can Immediately Update Fee Without Time-Lock, Enabling Front-Running DoS on User Requests — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `setProviderFee` function in `Entropy.sol` allows any registered Entropy provider to update their `feeInWei` immediately and atomically with no time-lock, queuing mechanism, or delay. Because `requestHelper` reads the live `feeInWei` at the moment a user's request transaction executes, a provider can front-run any pending user request by raising their fee in the same block, causing the user's transaction to revert with `InsufficientFee`. This is a direct structural analog to the Celo `updateCommission` vulnerability: a privileged-but-permissionless actor can manipulate a fee parameter at any time with no notice period, to the detriment of users who relied on the previously observed value.

---

### Finding Description

**Root cause — `setProviderFee` (and `setProviderFeeAsFeeManager`) apply immediately:**

```solidity
// Entropy.sol line 810–827
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) revert EntropyErrors.NoSuchProvider();
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;          // ← immediate, no delay
    emit ProviderFeeUpdated(msg.sender, oldFeeInWei, newFeeInWei);
    ...
}
``` [1](#0-0) 

There is no queuing, no minimum notice period, and no cap on how large the new fee can be. The same immediate-update path exists via `setProviderFeeAsFeeManager`. [2](#0-1) 

**Fee is validated at execution time in `requestHelper`:**

```solidity
// Entropy.sol line 234–235
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
``` [3](#0-2) 

`getFeeV2` reads `provider.feeInWei` live from storage. If the provider raised their fee between the user's off-chain `getFee()` call and the user's on-chain `requestWithCallback` / `requestV2` transaction, the user's transaction reverts. [4](#0-3) 

**Excess ETH is never refunded:**

```solidity
// Entropy.sol line 238–239
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [5](#0-4) 

The IEntropy interface explicitly documents: *"Note that excess value is not refunded to the caller."* [6](#0-5) 

**Provider registration is permissionless:**

Anyone can call `register()` to become a provider and then call `setProviderFee` at will. [7](#0-6) 

---

### Impact Explanation

A malicious (or compromised) provider can:

1. **DoS targeted users**: Monitor the mempool for pending `requestWithCallback` / `requestV2` transactions directed at their provider address. Submit a `setProviderFee(type(uint128).max)` transaction with higher gas priority in the same block. The user's transaction executes after the fee update and reverts with `InsufficientFee`. The user loses gas but not ETH (revert). Repeated across every pending request, this constitutes a sustained denial-of-service against all users of that provider.

2. **Trap users who send excess ETH**: A user or integrating contract that sends `msg.value` significantly above the quoted fee (a common defensive pattern) will have the excess permanently captured as Pyth protocol fees — not refunded — if the provider lowers their fee between the quote and the request. While this does not benefit the provider directly, it permanently destroys user funds.

3. **Undermine integrating smart contracts**: Many Entropy consumer contracts (as shown in the SDK documentation and `EntropyTester.sol`) follow the pattern of calling `getFee()` off-chain and then submitting a transaction with exactly that value. This pattern is broken by any fee change between the two steps. [8](#0-7) 

---

### Likelihood Explanation

- **Entry path is permissionless**: Any address can call `register()` and become a provider. No governance approval or stake is required.
- **Front-running is straightforward on EVM chains**: The attacker (provider) simply submits `setProviderFee` with a higher `gasPrice`/priority fee than the victim's request transaction. This is a standard mempool front-run requiring no special infrastructure beyond a standard MEV bot or even manual monitoring.
- **The Fortuna keeper itself demonstrates the pattern**: The off-chain `adjust_fee_wrapper` loop in `apps/fortuna/src/keeper/fee.rs` periodically calls `set_provider_fee` on-chain, confirming that fee changes are a normal operational action — making a malicious provider indistinguishable from a legitimate one until the attack occurs. [9](#0-8) 

---

### Recommendation

Implement a two-step, time-locked fee update mechanism analogous to the fix applied to the Celo `updateCommission` bug:

1. **Queue the fee update**: When a provider calls `setProviderFee(newFee)`, store the pending fee and the block number (or timestamp) at which it was queued. Do not apply it immediately.
2. **Apply after a delay**: Allow the fee to take effect only after a minimum number of blocks (e.g., one full epoch or a fixed block delay such as 50–100 blocks) have elapsed since the queue transaction.
3. **Enforce the delay in `requestHelper`**: Read the pending fee only if `block.number >= queuedAt + delay`; otherwise use the current active fee.

This gives users and integrating contracts a guaranteed observation window to react to fee changes before they take effect, eliminating the front-running vector entirely.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

interface IEntropy {
    function getFee(address provider) external view returns (uint128);
    function setProviderFee(uint128 newFeeInWei) external;
    function requestWithCallback(address provider, bytes32 userRandomNumber)
        external payable returns (uint64);
}

contract AttackingProvider {
    IEntropy entropy;
    address victim;

    constructor(address _entropy, address _victim) {
        entropy = IEntropy(_entropy);
        victim = _victim;
    }

    // Step 1: Provider registers with a low fee to attract users (done externally).

    // Step 2: Provider monitors mempool. When victim's requestWithCallback tx is seen,
    //         provider submits this function with higher gas priority in the same block.
    function frontRunFeeIncrease() external {
        // Raise fee to max — victim's pending tx will revert with InsufficientFee
        entropy.setProviderFee(type(uint128).max);
    }

    // Step 3 (optional): After victim's tx reverts, restore low fee to attract next victim.
    function restoreLowFee(uint128 normalFee) external {
        entropy.setProviderFee(normalFee);
    }
}

// Victim contract (standard Entropy consumer pattern from SDK docs):
contract VictimConsumer {
    IEntropy entropy;
    address provider;

    constructor(address _entropy, address _provider) {
        entropy = IEntropy(_entropy);
        provider = _provider;
    }

    // User calls this after querying getFee() off-chain.
    // If provider front-runs with frontRunFeeIncrease(), this reverts with InsufficientFee.
    function requestRandomness(bytes32 userRandomNumber) external payable {
        uint128 fee = entropy.getFee(provider); // reads current fee at execution time
        // msg.value was set based on the fee observed BEFORE the front-run.
        // Now fee == type(uint128).max, so msg.value < fee → revert.
        entropy.requestWithCallback{value: fee}(provider, userRandomNumber);
    }
}
```

The `setProviderFee` call at line 819 takes effect immediately with no delay, and `requestHelper` at line 234–235 reads the updated fee, causing the victim's transaction to revert. [10](#0-9) [11](#0-10)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L111-145)
```text
    function register(
        uint128 feeInWei,
        bytes32 commitment,
        bytes calldata commitmentMetadata,
        uint64 chainLength,
        bytes calldata uri
    ) public override {
        if (chainLength == 0) revert EntropyErrors.AssertionFailure();

        EntropyStructsV2.ProviderInfo storage provider = _state.providers[
            msg.sender
        ];

        // NOTE: this method implementation depends on the fact that ProviderInfo will be initialized to all-zero.
        // Specifically, accruedFeesInWei is intentionally not set. On initial registration, it will be zero,
        // then on future registrations, it will be unchanged. Similarly, provider.sequenceNumber defaults to 0
        // on initial registration.

        provider.feeInWei = feeInWei;

        provider.originalCommitment = commitment;
        provider.originalCommitmentSequenceNumber = provider.sequenceNumber;
        provider.currentCommitment = commitment;
        provider.currentCommitmentSequenceNumber = provider.sequenceNumber;
        provider.commitmentMetadata = commitmentMetadata;
        provider.endSequenceNumber = provider.sequenceNumber + chainLength;
        provider.uri = uri;

        provider.sequenceNumber += 1;

        emit EntropyEvents.Registered(
            EntropyStructConverter.toV1ProviderInfo(provider)
        );
        emit EntropyEventsV2.Registered(msg.sender, bytes(""));
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-235)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L237-239)
```text
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L760-794)
```text
    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
    }

    function getProviderFee(
        address providerAddr,
        uint32 gasLimit
    ) internal view returns (uint128 feeAmount) {
        EntropyStructsV2.ProviderInfo memory provider = _state.providers[
            providerAddr
        ];

        // Providers charge a minimum of their configured feeInWei for every request.
        // Requests using more than the defaultGasLimit get a proportionally scaled fee.
        // This approach may be somewhat simplistic, but it allows us to continue using the
        // existing feeInWei parameter for the callback failure flow instead of defining new
        // configuration values.
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
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L810-827)
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
        emit EntropyEventsV2.ProviderFeeUpdated(
            msg.sender,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L829-855)
```text
    function setProviderFeeAsFeeManager(
        address provider,
        uint128 newFeeInWei
    ) external override {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];

        if (providerInfo.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }

        if (providerInfo.feeManager != msg.sender) {
            revert EntropyErrors.Unauthorized();
        }

        uint128 oldFeeInWei = providerInfo.feeInWei;
        providerInfo.feeInWei = newFeeInWei;

        emit ProviderFeeUpdated(provider, oldFeeInWei, newFeeInWei);
        emit EntropyEventsV2.ProviderFeeUpdated(
            provider,
            oldFeeInWei,
            newFeeInWei,
            bytes("")
        );
    }
```

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropy.sol (L61-71)
```text
    // The `entropyCallback` method on that interface will receive a callback with the generated random number.
    // `entropyCallback` will be run with the provider's default gas limit (see `getProviderInfo(provider).defaultGasLimit`).
    // If your callback needs additional gas, please use the function `requestv2` from `IEntropyV2` interface
    // with gasLimit as the input parameter.
    //
    // This method will revert unless the caller provides a sufficient fee (at least `getFee(provider)`) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function requestWithCallback(
        address provider,
        bytes32 userRandomNumber
    ) external payable returns (uint64 assignedSequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyTester.sol (L61-68)
```text
        uint128 fee = entropy.getFee(provider);
        sequenceNumber = entropy.requestWithCallback{value: fee}(
            provider,
            // Hardcoding the user contribution because we don't really care for testing the callback.
            // Real users should pass this value in as an argument from the calling function.
            bytes32(uint256(12345))
        );

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
