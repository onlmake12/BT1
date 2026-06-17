### Title
Excess `msg.value` in Entropy Requests Is Silently Absorbed Into Protocol Fees Instead of Being Refunded to Caller — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary
In `Entropy.sol`, when a caller sends more `msg.value` than the required fee for `request`, `requestWithCallback`, or `requestV2`, the entire excess is silently credited to `_state.accruedPythFeesInWei` (the Pyth protocol treasury) rather than being refunded to `msg.sender`. Any overpayment is permanently lost to the caller.

### Finding Description
In `requestHelper`, the fee accounting is:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
```

The entire `msg.value - providerFee` — which includes any overpayment — is added to `_state.accruedPythFeesInWei`. There is no refund path back to `msg.sender`. The public-facing functions `request`, `requestWithCallback`, and `requestV2` all explicitly document this: *"Note that excess value is not refunded to the caller."*

The test suite confirms this behavior: a call that overpays by 10,000 wei results in that 10,000 wei being added to `getAccruedPythFees()`, not returned to the user. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
Any caller who sends more than the exact required fee permanently loses the excess to the Pyth protocol treasury. This is a direct, irreversible loss of user funds. The impact scales with the overpayment amount. Integrating contracts that add a fee buffer (a common defensive pattern when fees can change between query and submission) will silently lose that buffer on every request.

### Likelihood Explanation
The scenario is realistic for:
1. Smart contracts that add a small buffer to `getFeeV2()` to guard against fee changes between the query block and the execution block.
2. Contracts that hardcode a fee value that becomes stale after a provider updates their fee.
3. Users or contracts that round up the fee for simplicity.

The `requestV2` family of functions is the primary Entropy entry point for all integrators, making this a high-exposure surface.

### Recommendation
After deducting the required fee, refund any excess `msg.value` to `msg.sender`:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (requiredFee - providerFee);
// Refund excess to caller
uint256 excess = msg.value - requiredFee;
if (excess > 0) {
    (bool sent, ) = msg.sender.call{value: excess}("");
    require(sent, "Refund failed");
}
```

### Proof of Concept
1. Provider registers with `feeInWei = 100 wei`. Pyth fee is `50 wei`. Total required fee = `150 wei`.
2. An integrating contract calls `requestV2{value: 200 wei}(provider, userRandom, 0)` (adding a 50 wei buffer).
3. `requestHelper` executes: `providerInfo.accruedFeesInWei += 100`, `_state.accruedPythFeesInWei += (200 - 100) = 100`.
4. The 50 wei buffer is silently absorbed into Pyth fees. The caller receives no refund.
5. Confirmed by the existing test: `requestWithFee(user2, pythFeeInWei + provider2FeeInWei + 10000, ...)` → `assertEq(random.getAccruedPythFees(), pythFeeInWei * 5 + 10000)`. [4](#0-3) [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L320-322)
```text
    // This method will revert unless the caller provides a sufficient fee (at least getFee(provider)) as msg.value.
    // Note that excess value is *not* refunded to the caller.
    function request(
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
