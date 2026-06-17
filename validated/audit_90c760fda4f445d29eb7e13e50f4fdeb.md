### Title
Excess ETH Sent to Entropy Request Functions Is Permanently Absorbed Into Protocol Treasury Without Refund - (File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol)

### Summary
The `requestHelper()` function in `Entropy.sol`, which underlies all user-facing request entry points (`request()`, `requestWithCallback()`, `requestV2()`), accepts any `msg.value >= requiredFee` but silently distributes the entire `msg.value` between the provider's accrued fees and Pyth's `accruedPythFeesInWei`. Any excess ETH above `requiredFee` is permanently transferred to Pyth's treasury with no refund path for the caller.

### Finding Description
In `requestHelper()`, the fee check is a one-sided lower bound:

```solidity
uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
uint128 providerFee = getProviderFee(provider, callbackGasLimit);
providerInfo.accruedFeesInWei += providerFee;
_state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) - providerFee);
``` [1](#0-0) 

The entire `msg.value` is consumed: `providerFee` goes to the provider's balance, and `msg.value - providerFee` (which includes any overpayment) goes to `accruedPythFeesInWei`. There is no branch that returns excess ETH to `msg.sender`. The same pattern applies to `updatePriceFeeds()` in `Pyth.sol`, where excess ETH simply accumulates in the contract balance without being tracked or refunded. [2](#0-1) 

The behavior is confirmed by the existing test suite, which explicitly demonstrates that a 10,000 wei overpayment on a request is absorbed into `accruedPythFees` rather than returned to the user: [3](#0-2) 

The interface documentation acknowledges this: "Note that excess value is *not* refunded to the caller." [4](#0-3) 

### Impact Explanation
Any user or integrating contract that sends more than the exact `getFeeV2()` amount permanently loses the excess ETH to Pyth's treasury. The funds are not locked (Pyth admin can withdraw via `withdrawFee()`), but the original sender has no recovery path. This is a direct, irreversible loss of user funds. Integrators who add a small ETH buffer to avoid `InsufficientFee` reverts (a common defensive pattern) will silently lose that buffer on every call. [5](#0-4) 

### Likelihood Explanation
The likelihood is medium-to-high. Fees can change between the time a user queries `getFeeV2()` and the time their transaction is mined (provider re-registration). Smart contract integrators commonly add a small ETH buffer to guard against this race condition. EOA users relying on wallet fee estimation may also overpay. Every such overpayment results in a silent, permanent fund loss with no on-chain indication to the user. [6](#0-5) 

### Recommendation
Two mitigations, analogous to the external report's recommendations:

1. **Strict equality check**: Change the fee check to `if (msg.value != requiredFee) revert EntropyErrors.IncorrectFee();` — this forces callers to send exactly the required amount, preventing silent loss.

2. **Refund excess**: After distributing fees, refund any surplus to `msg.sender`:
```solidity
uint128 excess = SafeCast.toUint128(msg.value) - requiredFee;
if (excess > 0) {
    (bool ok, ) = msg.sender.call{value: excess}("");
    require(ok, "refund failed");
}
```

Option 2 is preferred for usability. `PythLazer.sol` already implements this pattern correctly and can serve as a reference: [7](#0-6) 

### Proof of Concept
1. Provider `P` is registered with `providerFeeInWei = 1000` and `pythFeeInWei = 100`. `requiredFee = 1100 wei`.
2. User calls `requestV2(P, randomNum, gasLimit)` with `msg.value = 1200 wei` (100 wei buffer to avoid revert).
3. `requestHelper` executes: `providerInfo.accruedFeesInWei += 1000`; `accruedPythFeesInWei += 200` (1200 - 1000).
4. The 100 wei overpayment is silently credited to Pyth's treasury. The user's balance is reduced by 1200 wei instead of 1100 wei.
5. The user has no mechanism to recover the 100 wei. Only the Pyth admin can access it via `withdrawFee()`. [1](#0-0)

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

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L77-79)
```text
        uint requiredFee = getTotalFee(totalNumUpdates);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();
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

**File:** target_chains/ethereum/entropy_sdk/solidity/IEntropyV2.sol (L67-69)
```text
    /// This method will revert unless the caller provides a sufficient fee (at least `getFeeV2(provider, gasLimit)`) as msg.value.
    /// Note that provider fees can change over time. Callers of this method should explicitly compute `getFeeV2(provider, gasLimit)`
    /// prior to each invocation (as opposed to hardcoding a value). Further note that excess value is *not* refunded to the caller.
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

**File:** lazer/contracts/evm/src/PythLazer.sol (L73-77)
```text
        // Require fee and refund excess
        require(msg.value >= verification_fee, "Insufficient fee provided");
        if (msg.value > verification_fee) {
            payable(msg.sender).transfer(msg.value - verification_fee);
        }
```
