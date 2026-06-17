### Title
Entropy Provider Can Frontrun User Requests via Unbounded `setProviderFee` to Cause DoS - (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol` allows any registered provider to call `setProviderFee` (or `setProviderFeeAsFeeManager`) to set their fee to any arbitrary `uint128` value with no upper bound, no timelock, and no delay. A malicious provider can frontrun a user's `requestWithCallback` / `requestV2` transaction by raising the fee above the user's `msg.value`, causing the user's transaction to revert with `InsufficientFee`. Provider registration is permissionless, so this attack requires no privileged access.

---

### Finding Description

`setProviderFee` in `Entropy.sol` performs only a single check — that the caller is a registered provider — before immediately writing any arbitrary `newFeeInWei` value to storage:

```solidity
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) {
        revert EntropyErrors.NoSuchProvider();
    }
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;
    ...
}
``` [1](#0-0) 

There is no maximum fee cap, no timelock, and no delay. The same is true for `setProviderFeeAsFeeManager`: [2](#0-1) 

On the user side, `requestHelper` reads the live fee at execution time and reverts if `msg.value` is insufficient:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
``` [3](#0-2) 

Because the fee is read at execution time and not at the time the user queried `getFeeV2`, a provider can atomically raise the fee between the user's off-chain `getFeeV2` call and the on-chain execution of `requestWithCallback` / `requestV2`.

The IEntropyV2 interface itself acknowledges this race condition with the warning:

> "Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()` prior to each invocation... Further note that excess value is *not* refunded to the caller." [4](#0-3) 

The same unbounded `setProviderFee` pattern exists in `Echo.sol`: [5](#0-4) 

---

### Impact Explanation

**Primary impact — DoS of user requests**: A malicious provider raises their fee to `type(uint128).max` (or any value exceeding the user's `msg.value`) just before the user's transaction lands. The user's `requestWithCallback` / `requestV2` reverts with `InsufficientFee`. The user loses gas but not ETH (ETH is returned on revert). However, if the user is a smart contract that hardcodes or caches the fee, all future requests to that provider are permanently broken until the provider lowers the fee.

**Secondary impact — forced overpayment**: A user who sends excess ETH as a buffer (a common defensive pattern) can have that buffer captured. The excess `msg.value` above `providerFee` accrues to Pyth fees and is never refunded. A provider who raises their fee to exactly match the user's `msg.value` causes the user to pay the full amount while the provider captures the maximum share. The user still receives the service but pays more than the quoted fee.

---

### Likelihood Explanation

Provider registration is permissionless — anyone can call `register` to become a provider. A malicious actor registers as a provider, attracts users (e.g., by advertising a low fee), then frontruns their requests. On EVM chains with a public mempool, frontrunning is straightforward: the provider monitors the mempool for `requestWithCallback` transactions targeting their address and submits a `setProviderFee` transaction with a higher gas price. This is a realistic, low-cost attack requiring no special access.

---

### Recommendation

1. **Introduce a maximum fee cap**: Add a protocol-level constant (e.g., `MAX_PROVIDER_FEE`) that `setProviderFee` enforces with a `require`.
2. **Add a timelock / staged fee update**: Require fee increases to be announced at least N blocks in advance (e.g., a two-step `proposeFee` / `applyFee` pattern). Fee decreases can remain immediate.
3. **Allow users to specify a `maxFee` parameter**: Add an optional `maxFeeInWei` argument to `requestWithCallback` / `requestV2` that causes the transaction to revert if the live fee exceeds the user's tolerance, rather than silently consuming excess ETH.

---

### Proof of Concept

```solidity
// 1. Attacker registers as a provider with a low fee (e.g., 1 wei)
entropy.register(1, providerCommitment, chainlengthBytes, 1000, uri);

// 2. Victim queries the fee and prepares a request
uint128 fee = entropy.getFeeV2(attackerProvider, 0); // returns pythFeeInWei + 1

// 3. Victim submits requestWithCallback{value: fee}(attackerProvider, userContribution)
//    (transaction is now in the mempool)

// 4. Attacker sees the pending transaction and frontruns it:
entropy.setProviderFee(type(uint128).max / 2); // fee now >> victim's msg.value

// 5. Victim's transaction executes AFTER the fee update:
//    getFeeV2(attackerProvider, 0) >> msg.value
//    → revert EntropyErrors.InsufficientFee()
//    Victim loses gas; request is never fulfilled.
```

The `setProviderFee` call has no upper bound check: [6](#0-5) 

The fee is consumed at execution time with no slippage protection for the user: [7](#0-6)

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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L17-19)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2()`) as msg.value.
    /// Note that the fee can change over time. Callers of this method should explicitly compute `getFeeV2()`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L395-426)
```text
    function setProviderFee(
        address provider,
        uint96 newBaseFeeInWei,
        uint96 newFeePerFeedInWei,
        uint96 newFeePerGasInWei
    ) external override {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );
        require(
            msg.sender == provider ||
                msg.sender == _state.providers[provider].feeManager,
            "Only provider or fee manager can invoke this method"
        );

        uint96 oldBaseFee = _state.providers[provider].baseFeeInWei;
        uint96 oldFeePerFeed = _state.providers[provider].feePerFeedInWei;
        uint96 oldFeePerGas = _state.providers[provider].feePerGasInWei;
        _state.providers[provider].baseFeeInWei = newBaseFeeInWei;
        _state.providers[provider].feePerFeedInWei = newFeePerFeedInWei;
        _state.providers[provider].feePerGasInWei = newFeePerGasInWei;
        emit ProviderFeeUpdated(
            provider,
            oldBaseFee,
            oldFeePerFeed,
            oldFeePerGas,
            newBaseFeeInWei,
            newFeePerFeedInWei,
            newFeePerGasInWei
        );
    }
```
