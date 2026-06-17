### Title
Provider Can Frontrun User's `requestV2` by Raising Fee to Cause `InsufficientFee` Revert — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

An Entropy provider can frontrun a user's `requestV2` call by calling `setProviderFee` to increase their fee by even 1 wei, causing the user's transaction to revert with `InsufficientFee`. This is the direct analog of M-6: the target of an action slightly modifies state so that the caller's exact parameter fails a check, causing a revert without resolving the underlying situation.

---

### Finding Description

In `requestHelper`, the required fee is computed at execution time from the provider's current `feeInWei`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
``` [1](#0-0) 

The fee is computed as `provider.feeInWei + _state.pythFeeInWei`:

```solidity
function getFeeV2(address provider, uint32 gasLimit) public view override returns (uint128 feeAmount) {
    return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
}
``` [2](#0-1) 

The provider can change `feeInWei` at any time with no delay or timelock:

```solidity
function setProviderFee(uint128 newFeeInWei) external override {
    ...
    provider.feeInWei = newFeeInWei;
``` [3](#0-2) 

The `requestV2` interface accepts no `maxFee` parameter — the user simply sends `msg.value`:

```solidity
function requestV2(
    address provider,
    bytes32 userRandomNumber,
    uint32 gasLimit
) external payable returns (uint64 assignedSequenceNumber);
``` [4](#0-3) 

The same pattern exists in the Echo contract's `requestPriceUpdatesWithCallback`:

```solidity
uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
if (msg.value < requiredFee) revert InsufficientFee();
``` [5](#0-4) 

with the provider fee also changeable at any time: [6](#0-5) 

---

### Impact Explanation

A user who calls `getFeeV2(provider, gasLimit)` off-chain and submits `requestV2{value: fee}(...)` can have their transaction reverted if the provider raises their fee by even 1 wei between the user's fee query and the transaction's inclusion in a block. The user receives no randomness and loses gas. The attack can be repeated indefinitely, permanently denying a specific user access to the provider's randomness service without the provider ever needing to make their position "healthy" (i.e., the provider does not need to deregister or stop serving other users). Funds are not lost (the transaction reverts), but the service is temporarily — and repeatably — denied.

---

### Likelihood Explanation

The attack requires only that the provider submit a `setProviderFee` transaction with a higher gas price than the user's `requestV2` transaction (standard frontrunning). The provider has a plausible incentive: they may wish to force users to retry at a higher fee, extracting more value. The `fortuna` keeper already calls `set_provider_fee_as_fee_manager` automatically as part of fee adjustment logic: [7](#0-6) 

This means fee changes are a normal operational event, and any fee increase — even an automated one — can race with a pending user request and cause it to revert.

---

### Recommendation

Add a `maxFee` parameter to `requestV2` (and `requestPriceUpdatesWithCallback` in Echo) so callers can specify the maximum fee they are willing to pay. If `requiredFee > maxFee`, revert with a descriptive error. This is analogous to slippage protection in DEX swaps. Alternatively, document and enforce a minimum fee-change notice period (timelock) before a provider's new fee takes effect.

---

### Proof of Concept

1. User queries `fee = getFeeV2(provider, gasLimit)` off-chain. Suppose `fee = 1000 wei`.
2. User submits `requestV2{value: 1000}(provider, userRandom, gasLimit)`.
3. Provider observes the pending transaction in the mempool and submits `setProviderFee(1001)` with a higher gas price (frontrun).
4. Provider's `setProviderFee` lands first. Now `getFeeV2(provider, gasLimit) = 1001 wei`.
5. User's `requestV2` executes: `requiredFee = 1001`, `msg.value = 1000` → `msg.value < requiredFee` → `revert EntropyErrors.InsufficientFee()`.
6. User's transaction reverts. No randomness is generated. The provider can repeat this for every retry. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L233-235)
```text
        // Check that fees were paid and increment the pyth / provider balances.
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L97-101)
```text
    function requestV2(
        address provider,
        bytes32 userRandomNumber,
        uint32 gasLimit
    ) external payable returns (uint64 assignedSequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L75-76)
```text
        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();
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

**File:** apps/fortuna/src/keeper/fee.rs (L381-382)
```rust
        let contract_call = contract.set_provider_fee_as_fee_manager(provider_address, target_fee);
        send_and_confirm(contract_call).await?;
```
