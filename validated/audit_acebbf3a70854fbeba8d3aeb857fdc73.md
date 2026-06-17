### Title
Insufficient Gas Sufficiency Check in `revealWithCallback` Does Not Account for EIP-2929 Cold Account Access Costs — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.revealWithCallback`, after calling `req.requester.excessivelySafeCall(gasLimit, ...)`, the contract uses the check `(startingGas * 31) / 32 > gasLimit` to decide whether the callback was provided sufficient gas. This check uses `31/32` as a conservative proxy for the EVM's `63/64` rule, but it does not account for the EIP-2929 cold account access cost (2500 gas) that is deducted from available gas *before* the `63/64` rule is applied. For small gas limits (below ~155,000), the `31/32` margin is insufficient to cover this cold access cost, causing the contract to incorrectly classify a genuine out-of-gas failure as a legitimate `CALLBACK_FAILED` state.

---

### Finding Description

In `revealWithCallback`, when `req.gasLimit10k != 0` and `callbackStatus == CALLBACK_NOT_STARTED`, the contract:

1. Records `startingGas = gasleft()` before the call.
2. Calls `req.requester.excessivelySafeCall(uint256(req.gasLimit10k) * TEN_THOUSAND, ...)`.
3. After the call, checks whether the callback was provided sufficient gas:

```solidity
} else if (
    (startingGas * 31) / 32 >
    uint256(req.gasLimit10k) * TEN_THOUSAND
) {
    // ... emit CallbackFailed, set CALLBACK_FAILED
} else {
    revert EntropyErrors.InsufficientGas();
}
``` [1](#0-0) 

The comment acknowledges the `63/64` rule but claims `31/32` provides a "margin of safety": [2](#0-1) 

The extra margin between `31/32` and `63/64` is exactly `1/64 * startingGas`. For this margin to cover the EIP-2929 cold account access cost of 2500 gas (= `ColdAccountAccessCostEIP2929 - WarmStorageReadCostEIP2929 = 2600 - 100`), the following must hold:

```
startingGas / 64 > 2500
startingGas > 160,000
```

Since `startingGas ≈ gasLimit * 32/31` at the minimum passing threshold, this means the margin is only sufficient when `gasLimit > ~155,000`.

The actual gas forwarded to the callback is:

```
min(gasLimit, 63/64 * (startingGas - overhead - cold_access_cost))
```

where `cold_access_cost = 2500` if `req.requester` is a cold address (not yet in the EIP-2929 access list for this transaction). Since `revealWithCallback` is called by the provider (Fortuna keeper) and `req.requester` is the user's contract — never accessed earlier in the same transaction — it is always cold on the first reveal.

For `gasLimit < ~155,000`, there exists a range of `startingGas` values where:
- `startingGas * 31/32 > gasLimit` (check passes → contract concludes callback had enough gas)
- `63/64 * (startingGas - 2500 - overhead) < gasLimit` (callback actually ran out of gas)

In this scenario, the contract emits `CallbackFailed` and sets `req.callbackStatus = CALLBACK_FAILED`, even though the root cause was insufficient gas forwarding, not a legitimate callback revert. [3](#0-2) 

---

### Impact Explanation

When the check misclassifies an out-of-gas failure as `CALLBACK_FAILED`:

1. The request is permanently moved to `CALLBACK_FAILED` state.
2. The subsequent retry path (when `callbackStatus == CALLBACK_FAILED`) falls into the `else` branch, which calls the callback **directly** (no gas limit enforcement) and **clears the request first**:

```solidity
clearRequest(provider, sequenceNumber);
// ...
IEntropyConsumer(callAddress)._entropyCallback(...);
``` [4](#0-3) 

If the retry also fails (e.g., the provider again provides borderline gas), the request is already cleared and the user permanently loses their randomness fulfillment. Even if the retry succeeds, the user's callback was silently skipped on the first attempt, which may have caused application-level state inconsistencies (e.g., a DeFi protocol that expected the callback to execute atomically with the reveal).

The `InsufficientGas` revert path — which would have allowed the provider to retry with more gas — is bypassed entirely.

---

### Likelihood Explanation

- `req.requester` is always a cold address on the first `revealWithCallback` call (the provider's transaction does not access it beforehand).
- Provider default gas limits can be as low as 10,000 gas (the minimum rounded unit), well below the ~155,000 threshold.
- Any user who sets a custom gas limit below ~155,000 via `requestV2(gasLimit)` is affected.
- The Fortuna keeper calls `revealWithCallback` without any special gas padding beyond what is needed to pass the `31/32` check, making borderline gas scenarios realistic. [5](#0-4) 

---

### Recommendation

Account for the EIP-2929 cold account access cost in the gas sufficiency check. Replace:

```solidity
(startingGas * 31) / 32 > uint256(req.gasLimit10k) * TEN_THOUSAND
```

with:

```solidity
(startingGas * 63) / 64 > uint256(req.gasLimit10k) * TEN_THOUSAND + 2600
```

The `2600` accounts for `ColdAccountAccessCostEIP2929`. Alternatively, warm up `req.requester` before the call (e.g., via an `extcodesize` check or by adding it to the access list), so the cold access cost is paid before `startingGas` is recorded.

---

### Proof of Concept

Consider a provider with `defaultGasLimit = 100,000`. The minimum `startingGas` to pass the `31/32` check is:

```
startingGas > 100,000 * 32/31 ≈ 103,226
```

The extra margin is `103,226 / 64 ≈ 1,613` gas — less than the 2,500 cold access cost.

Actual gas forwarded to callback:
```
63/64 * (103,226 - ~100 overhead - 2,500) ≈ 63/64 * 100,626 ≈ 99,053
```

This is less than `gasLimit = 100,000`. The callback runs out of gas, but the check `103,226 * 31/32 = 100,031 > 100,000` passes, so the contract emits `CallbackFailed` and sets `CALLBACK_FAILED` instead of reverting with `InsufficientGas`. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L578-596)
```text
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
            bool success;
            bytes memory ret;
            uint256 startingGas = gasleft();
            (success, ret) = req.requester.excessivelySafeCall(
                // Warning: the provided gas limit below is only an *upper bound* on the gas provided to the call.
                // At most 63/64ths of the current context's gas will be provided to a call, which may be less
                // than the indicated gas limit. (See CALL opcode docs here https://www.evm.codes/?fork=cancun#f1)
                // Consequently, out-of-gas reverts need to be handled carefully to ensure that the callback
                // was truly provided with a sufficient amount of gas.
                uint256(req.gasLimit10k) * TEN_THOUSAND,
                256, // copy at most 256 bytes of the return value into ret.
                abi.encodeWithSelector(
                    IEntropyConsumer._entropyCallback.selector,
                    sequenceNumber,
                    provider,
                    randomNumber
                )
            );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-659)
```text
            } else if (
                (startingGas * 31) / 32 >
                uint256(req.gasLimit10k) * TEN_THOUSAND
            ) {
                // The callback reverted for some reason.
                // We don't use ret to condition the behavior here (out-of-gas or other revert), as we have found that some user contracts
                // catch out-of-gas errors and revert with a different error.
                // In this case, ensure that the callback was provided with sufficient gas. Technically, 63/64ths of the startingGas is forwarded,
                // but we're using 31/32 to introduce a margin of safety.
                emit CallbackFailed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    userContribution,
                    providerContribution,
                    randomNumber,
                    ret
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    true,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-681)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

            // Check if the requester is a contract account.
            uint len;
            assembly {
                len := extcodesize(callAddress)
            }
            uint256 startingGas = gasleft();
            if (len != 0) {
                IEntropyConsumer(callAddress)._entropyCallback(
                    sequenceNumber,
                    provider,
                    randomNumber
                );
            }
```
