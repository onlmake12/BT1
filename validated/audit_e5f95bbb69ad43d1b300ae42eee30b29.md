### Title
`getFee()` / `getFeeV2()` Return Misleading Non-Zero Values for Non-Existent Providers Without Reverting — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.sol`'s `getFee(address provider)` and `getFeeV2(address provider, uint32 gasLimit)` do not check whether the provider is registered before computing the fee. For a non-existent provider, both functions silently return `pythFeeInWei` (the Pyth protocol fee only, with zero provider fee) instead of reverting. This is the direct analog of the Sablier `isTransferable()` bug: a public view function missing an existence guard, returning a misleading default value.

---

### Finding Description

Every state-mutating function in `Entropy.sol` that touches provider state guards with:

```solidity
if (provider.sequenceNumber == 0) revert EntropyErrors.NoSuchProvider();
```

This guard appears in `setProviderFee`, `setProviderFeeAsFeeManager`, `setProviderUri`, `setFeeManager`, `setMaxNumHashes`, `setDefaultGasLimit`, `withdrawAsFeeManager`, and `requestHelper`.

However, the public view functions `getFee` and `getFeeV2(address, uint32)` are missing this guard entirely:

```solidity
function getFee(
    address provider
) public view override returns (uint128 feeAmount) {
    return getFeeV2(provider, 0);
}

function getFeeV2(
    address provider,
    uint32 gasLimit
) public view override returns (uint128 feeAmount) {
    return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
}
```

`getProviderFee` reads `_state.providers[providerAddr]` without any existence check:

```solidity
function getProviderFee(
    address providerAddr,
    uint32 gasLimit
) internal view returns (uint128 feeAmount) {
    EntropyStructsV2.ProviderInfo memory provider = _state.providers[
        providerAddr
    ];
    // ...
    return provider.feeInWei; // 0 for non-existent provider
}
```

For a non-existent provider, all fields are zero-initialized by the EVM, so `provider.feeInWei == 0` and `provider.defaultGasLimit == 0`. The function returns `0 + _state.pythFeeInWei = pythFeeInWei`.

Similarly, `getProviderInfo` and `getProviderInfoV2` return a fully zeroed struct for non-existent providers without reverting:

```solidity
function getProviderInfoV2(
    address provider
) public view override returns (EntropyStructsV2.ProviderInfo memory info) {
    info = _state.providers[provider]; // zeroed struct, no existence check
}
```

The Entropy test suite itself acknowledges this at line 608–610:

> *"Requesting the fee for a nonexistent provider returns pythFeeInWei. This isn't necessarily desirable behavior, but it's unlikely to cause a problem."*

---

### Impact Explanation

**On-chain impact:** A user or smart contract consumer that calls `getFee(nonExistentProvider)` receives `pythFeeInWei` as the fee estimate. If they then call `request{value: pythFeeInWei}(nonExistentProvider, ...)`, the transaction reverts with `NoSuchProvider`. ETH is returned on revert, but gas is wasted. Smart contract consumers that use low-level calls or do not properly handle reverts may silently fail or enter an inconsistent state.

**Off-chain impact:** The Fortuna keeper service calls `getProviderInfoV2(providerAddress)` to determine whether to re-register. For a zeroed struct, `end_sequence_number (0) <= sequence_number (0)` evaluates to `true`, causing Fortuna to attempt re-registration for a non-existent provider address — a silent operational error.

**Fee underpayment vector:** A consumer contract pattern of `entropy.request{value: entropy.getFee(provider)}(provider, ...)` will silently underpay (sending only `pythFeeInWei`) and revert if `provider` is unregistered. The contract's caller receives no useful error from the view call.

---

### Likelihood Explanation

Any user or contract that queries `getFee` or `getProviderInfo` for an unregistered address — due to a misconfiguration, stale address, typo, or address rotation — will receive a misleading result. The Entropy contract is permissionlessly accessible, and the pattern of calling `getFee` before `request` is the documented SDK usage pattern. The likelihood is **medium**: it requires a wrong provider address, which is plausible in production integrations.

---

### Recommendation

Add an existence check to `getFeeV2(address, uint32)` and `getProviderInfo`/`getProviderInfoV2`:

```solidity
function getFeeV2(
    address provider,
    uint32 gasLimit
) public view override returns (uint128 feeAmount) {
+   if (_state.providers[provider].sequenceNumber == 0)
+       revert EntropyErrors.NoSuchProvider();
    return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
}

function getProviderInfoV2(
    address provider
) public view override returns (EntropyStructsV2.ProviderInfo memory info) {
+   if (_state.providers[provider].sequenceNumber == 0)
+       revert EntropyErrors.NoSuchProvider();
    info = _state.providers[provider];
}
```

---

### Proof of Concept

```solidity
function test_getFeeDoesNotRevertForNonExistentProvider() public {
    address ghost = address(0xdead);
    // Returns pythFeeInWei instead of reverting — misleading
    assertEq(random.getFee(ghost), pythFeeInWei);

    // getProviderInfoV2 returns zeroed struct instead of reverting
    EntropyStructsV2.ProviderInfo memory info = random.getProviderInfoV2(ghost);
    assertEq(info.sequenceNumber, 0);
    assertEq(info.feeInWei, 0);

    // Consumer contract pattern: underpays and gets NoSuchProvider revert
    vm.expectRevert(EntropyErrors.NoSuchProvider.selector);
    random.request{value: pythFeeInWei}(ghost, bytes32(uint256(42)), false);
}
```

This test passes against the current codebase, confirming the root cause.

---

**Root cause references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L713-717)
```text
    function getProviderInfoV2(
        address provider
    ) public view override returns (EntropyStructsV2.ProviderInfo memory info) {
        info = _state.providers[provider];
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L744-765)
```text
    function getFee(
        address provider
    ) public view override returns (uint128 feeAmount) {
        return getFeeV2(provider, 0);
    }

    function getFeeV2() external view override returns (uint128 feeAmount) {
        return getFeeV2(getDefaultProvider(), 0);
    }

    function getFeeV2(
        uint32 gasLimit
    ) external view override returns (uint128 feeAmount) {
        return getFeeV2(getDefaultProvider(), gasLimit);
    }

    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L767-793)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L815-817)
```text
        if (provider.sequenceNumber == 0) {
            revert EntropyErrors.NoSuchProvider();
        }
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L605-611)
```text
    function testGetFee() public {
        assertEq(random.getFee(provider1), pythFeeInWei + provider1FeeInWei);
        assertEq(random.getFee(provider2), pythFeeInWei + provider2FeeInWei);
        // Requesting the fee for a nonexistent provider returns pythFeeInWei. This isn't necessarily desirable behavior,
        // but it's unlikely to cause a problem.
        assertEq(random.getFee(unregisteredProvider), pythFeeInWei);

```
