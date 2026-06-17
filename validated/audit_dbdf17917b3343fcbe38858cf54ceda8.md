### Title
Excess ETH Overpayment Permanently Captured as Protocol Fees Without Refund — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `requestHelper` function accepts `msg.value` for fee payment but never refunds any excess ETH to the caller. Any amount above the required fee is silently credited to `_state.accruedPythFeesInWei` (the Pyth protocol treasury), permanently removing it from the user's control. This is the direct analog to the li.fi refund-loss pattern: instead of a bridge refund being lost to the router, here excess ETH is lost to the protocol fee accumulator.

---

### Finding Description

`requestHelper` is the internal function called by all public request entry points (`request`, `requestWithCallback`, `requestV2`). Its fee accounting logic is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The entire `msg.value` is consumed: `providerFee` goes to the provider's accrued balance, and the remainder (`msg.value - providerFee`) goes to `accruedPythFeesInWei`. There is no branch that returns excess ETH to `msg.sender`. The interface documentation explicitly acknowledges this:

> "Note that excess value is *not* refunded to the caller." [2](#0-1) [3](#0-2) 

The `accruedPythFeesInWei` balance is only withdrawable by the admin via `withdrawFee()` in `EntropyGovernance.sol`: [4](#0-3) 

The existing test suite confirms overpayment is silently absorbed:

```solidity
// this call overpays for the random number
requestWithFee(user2, pythFeeInWei + provider2FeeInWei + 10000, provider2, 42, false);
// ...
assertEq(random.getAccruedPythFees(), pythFeeInWei * 5 + 10000);
``` [5](#0-4) 

---

### Impact Explanation

Any ETH sent above `getFeeV2(provider, callbackGasLimit)` is permanently transferred to the Pyth protocol treasury. The user has no mechanism to recover it. For integrating contracts that forward user funds (e.g., a wrapper that calls `requestV2{value: msg.value}()`), any overpayment by the end user is silently lost. The loss is proportional to the overpayment amount and is irreversible without admin intervention.

---

### Likelihood Explanation

This is a realistic and common scenario:

1. **Fee race condition**: A user queries `getFeeV2()` off-chain, then the provider or Pyth governance raises the fee before the transaction lands. The user's transaction reverts. To avoid this, users and integrating contracts routinely add a buffer (e.g., `msg.value = fee * 110 / 100`). That 10% buffer is permanently lost.
2. **Integrating contracts**: Contracts that pass `msg.value` directly (e.g., `entropy.requestV2{value: msg.value}(...)`) without computing the exact fee will overpay whenever the caller sends more than required.
3. **Documented behavior**: The interface explicitly documents this behavior, meaning integrators are expected to handle it — but many will not, leading to user fund loss.

---

### Recommendation

Refund excess ETH to `msg.sender` at the end of `requestHelper`:

```solidity
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool refunded, ) = msg.sender.call{value: excess}("");
    require(refunded, "Refund failed");
}
_state.accruedPythFeesInWei += (requiredFee - providerFee);
```

Alternatively, enforce `msg.value == requiredFee` exactly (revert on overpayment) to make the behavior explicit and prevent silent loss.

---

### Proof of Concept

1. Provider fee = 0.001 ETH, Pyth fee = 0.0001 ETH → `requiredFee` = 0.0011 ETH.
2. User calls `requestV2{value: 0.002 ETH}(provider, userRandom, gasLimit)`.
3. `requestHelper` executes: `providerInfo.accruedFeesInWei += 0.001 ETH`; `_state.accruedPythFeesInWei += 0.001 ETH` (instead of `0.0001 ETH`).
4. The 0.0009 ETH overpayment is credited to `accruedPythFeesInWei` and is only recoverable by the Pyth admin.
5. The user has permanently lost 0.0009 ETH with no on-chain recourse. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L214-239)
```text
    function requestHelper(
        address provider,
        bytes32 userCommitment,
        bool useBlockhash,
        bool isRequestWithCallback,
        uint32 callbackGasLimit
    ) internal returns (EntropyStructsV2.Request storage req) {
        EntropyStructsV2.ProviderInfo storage providerInfo = _state.providers[
            provider
        ];
        if (_state.providers[provider].sequenceNumber == 0)
            revert EntropyErrors.NoSuchProvider();

        // Assign a sequence number to the request
        uint64 assignedSequenceNumber = providerInfo.sequenceNumber;
        if (assignedSequenceNumber >= providerInfo.endSequenceNumber)
            revert EntropyErrors.OutOfRandomness();
        providerInfo.sequenceNumber += 1;

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
