### Title
Unbounded `setProviderFee` Allows Any Registered Entropy Provider to DoS Randomness Requests via Fee Overflow — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `setProviderFee` and `setProviderFeeAsFeeManager` functions in `Entropy.sol` accept any `uint128` value without an upper bound check. A registered Entropy provider (or their fee manager) can set `feeInWei = type(uint128).max`, causing `getFeeV2` to overflow and revert in Solidity 0.8+, permanently DoS-ing all randomness requests to that provider.

---

### Finding Description

`setProviderFee` writes the caller-supplied value directly to storage with no upper bound validation:

```solidity
function setProviderFee(uint128 newFeeInWei) external override {
    EntropyStructsV2.ProviderInfo storage provider = _state.providers[msg.sender];
    if (provider.sequenceNumber == 0) {
        revert EntropyErrors.NoSuchProvider();
    }
    uint128 oldFeeInWei = provider.feeInWei;
    provider.feeInWei = newFeeInWei;   // ← no upper bound
    ...
}
``` [1](#0-0) 

The same pattern exists in `setProviderFeeAsFeeManager`: [2](#0-1) 

When `feeInWei = type(uint128).max`, the fee aggregation in `getFeeV2` overflows:

```solidity
function getFeeV2(address provider, uint32 gasLimit) public view override returns (uint128 feeAmount) {
    return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
    //     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ type(uint128).max
    //                                        + pythFeeInWei  → overflow revert
}
``` [3](#0-2) 

`requestHelper` calls `getFeeV2` unconditionally before any user payment check:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
``` [4](#0-3) 

Because Solidity 0.8+ uses checked arithmetic, `type(uint128).max + pythFeeInWei` (where `pythFeeInWei > 0`) reverts before any user-supplied value is even compared. Even if `pythFeeInWei == 0`, `getFeeV2` returns `type(uint128).max`, which no realistic `msg.value` can satisfy, causing every request to revert with `InsufficientFee`.

The same overflow path exists in `getProviderFee` when `gasLimit > defaultGasLimit`, because the scaled fee computation `provider.feeInWei + additionalFee` also overflows: [5](#0-4) 

---

### Impact Explanation

Any user calling `request`, `requestWithCallback`, or `requestV2` targeting the affected provider will have their transaction revert. The provider's entire randomness service is permanently bricked until governance or the provider resets the fee. If the affected provider is the **default provider** (set by admin), then the no-argument `getFeeV2()` and `getFeeV2(uint32)` overloads also revert:

```solidity
function getFeeV2() external view override returns (uint128 feeAmount) {
    return getFeeV2(getDefaultProvider(), 0);
}
``` [6](#0-5) 

This breaks the default randomness request flow for all users who do not explicitly specify a provider address.

---

### Likelihood Explanation

Any address that has called `register()` on the Entropy contract is a valid provider and can call `setProviderFee` without any privileged access. Additionally, a provider's designated fee manager (set via `setFeeManager`) can call `setProviderFeeAsFeeManager` with the same unbounded value. The fee manager is a separate address from the provider, so a compromised or malicious fee manager can DoS the provider's service without the provider's consent. No governance key, leaked secret, or majority attack is required. [7](#0-6) 

---

### Recommendation

Add an upper bound check in both `setProviderFee` and `setProviderFeeAsFeeManager` to prevent values that would cause overflow when combined with `pythFeeInWei`:

```solidity
require(
    newFeeInWei <= type(uint128).max - _state.pythFeeInWei,
    "Fee would cause overflow"
);
```

Alternatively, define a protocol-level `MAX_PROVIDER_FEE` constant and enforce it in both setter functions.

---

### Proof of Concept

1. Attacker registers as an Entropy provider (permissionless).
2. Attacker calls `setProviderFee(type(uint128).max)`.
3. Any user calls `getFeeV2(attackerProvider, 0)`:
   - `getProviderFee` returns `type(uint128).max`
   - `type(uint128).max + pythFeeInWei` overflows → revert
4. Any user calls `request(attackerProvider, ...)` or `requestV2(attackerProvider, ...)`:
   - `requestHelper` calls `getFeeV2` → revert
   - Transaction fails; no randomness can be requested from this provider.
5. If admin has set `attackerProvider` as the default provider, `getFeeV2()` (no args) also reverts, breaking all default-provider randomness requests protocol-wide.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-236)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L750-752)
```text
    function getFeeV2() external view override returns (uint128 feeAmount) {
        return getFeeV2(getDefaultProvider(), 0);
    }
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L767-794)
```text
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
