### Title
`getFeeV2(address, uint32)` View Function Reverts Instead of Returning a Safe Value for Out-of-Range Gas Limits — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
`getFeeV2(address provider, uint32 gasLimit)` is a `view` function that callers are explicitly instructed to invoke before every `requestV2` call to determine the required fee. When `gasLimit` exceeds `MAX_GAS_LIMIT` (`uint16.max * 10_000 = 655,350,000`), the function reverts with `EntropyErrors.MaxGasLimitExceeded` instead of returning a safe value. Since `uint32` can hold values up to ~4.29 billion, a large portion of the valid `uint32` input space causes this view function to revert, breaking any integration that does not defensively wrap the call.

### Finding Description
`getFeeV2(address, uint32)` delegates to the internal `getProviderFee()`, which calls `roundTo10kGas(gasLimit)`. Inside `roundTo10kGas`, if `gas > MAX_GAS_LIMIT`, the function unconditionally reverts:

```solidity
function roundTo10kGas(uint32 gas) internal pure returns (uint16) {
    if (gas > MAX_GAS_LIMIT) {
        revert EntropyErrors.MaxGasLimitExceeded();
    }
    ...
}
```

`getFeeV2` is a `view` function documented as the canonical pre-flight fee query. Callers — including third-party smart contracts — are expected to call it with an arbitrary `uint32` gasLimit before submitting a request. When the gasLimit is in the range `(655_350_000, 4_294_967_295]`, the view call reverts rather than returning a meaningful value (e.g., the maximum supported fee, or reverting with a structured error that the caller can catch and interpret).

### Impact Explanation
Any smart contract that calls `getFeeV2(provider, gasLimit)` with a user-supplied or computed `gasLimit > MAX_GAS_LIMIT` will receive an unexpected revert. If the calling contract does not wrap the call in a try/catch, its own transaction will revert, potentially locking users out of the randomness request flow. This is the direct analog of `maxWithdraw()` reverting: a view function that is supposed to be a safe, always-returning query instead reverts, breaking integrations.

### Likelihood Explanation
Any unprivileged Entropy user or integrating contract that passes a `gasLimit` larger than `655,350,000` (e.g., a contract that forwards a user-supplied gas limit, or one that computes a large limit for a complex callback) will trigger the revert. The `uint32` type naturally allows values up to ~4.29 billion, so the revert-triggering range is large and reachable without any privileged access.

### Recommendation
`getFeeV2(address, uint32)` should not revert for any valid `uint32` input. Two options:
1. Cap the gasLimit silently at `MAX_GAS_LIMIT` and return the corresponding maximum fee.
2. Return a sentinel value (e.g., `type(uint128).max`) to signal that the gasLimit is unsupported, without reverting.

This mirrors the EIP-4626 recommendation: if the operation is entirely disabled or out of range, return 0 (or a bounded value) rather than reverting.

### Proof of Concept
```solidity
// Any caller passing gasLimit > 655_350_000 causes getFeeV2 to revert:
uint32 oversizedGasLimit = uint32(type(uint16).max) * 10_000 + 1; // 655_350_001
// This call reverts with EntropyErrors.MaxGasLimitExceeded:
entropy.getFeeV2(provider, oversizedGasLimit);
```

This is confirmed by the existing test suite:

```solidity
// From Entropy.t.sol line 1721-1722:
vm.expectRevert(EntropyErrors.MaxGasLimitExceeded.selector);
random.getFeeV2(provider1, exceedsGasLimit);
``` [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L760-764)
```text
    function getFeeV2(
        address provider,
        uint32 gasLimit
    ) public view override returns (uint128 feeAmount) {
        return getProviderFee(provider, gasLimit) + _state.pythFeeInWei;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L962-965)
```text
    function roundTo10kGas(uint32 gas) internal pure returns (uint16) {
        if (gas > MAX_GAS_LIMIT) {
            revert EntropyErrors.MaxGasLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/test/Entropy.t.sol (L1719-1728)
```text
        // Test larger than max value reverts with expected error
        uint32 exceedsGasLimit = uint32(type(uint16).max) * 10000 + 1;
        vm.expectRevert(EntropyErrors.MaxGasLimitExceeded.selector);
        random.getFeeV2(provider1, exceedsGasLimit);
        vm.expectRevert(EntropyErrors.MaxGasLimitExceeded.selector);
        random.requestV2{value: 10000000000000}(
            provider1,
            bytes32(uint(42)),
            exceedsGasLimit
        );
```
