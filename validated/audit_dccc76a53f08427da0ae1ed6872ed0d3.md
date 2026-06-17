### Title
`Entropy.revealWithCallback` Legacy Callback Path Lacks Fail-Safe Handling, Permanently Locking User Funds — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains two execution paths. The **new path** (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` to safely invoke the consumer callback. The **legacy path** (when `req.gasLimit10k == 0`) calls `IEntropyConsumer(callAddress)._entropyCallback(...)` directly with no try-catch or low-level call error handling. If the consumer contract reverts for any reason, the entire `revealWithCallback` transaction reverts — undoing the preceding `clearRequest` — leaving the request permanently active and the user's paid fee permanently locked.

---

### Finding Description

In `revealWithCallback`, the branching logic at line 574 selects between the new and legacy callback flows:

```solidity
if (
    req.gasLimit10k != 0 &&
    req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
) {
    // NEW FLOW: uses excessivelySafeCall — fail-safe
    (success, ret) = req.requester.excessivelySafeCall(...);
    ...
} else {
    // LEGACY FLOW: direct call — NOT fail-safe
    clearRequest(provider, sequenceNumber);   // effects first
    ...
    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(  // bare call, no try-catch
            sequenceNumber,
            provider,
            randomNumber
        );
    }
}
``` [1](#0-0) [2](#0-1) 

The legacy path is entered whenever `req.gasLimit10k == 0`, which is set at request time when the chosen provider has `providerInfo.defaultGasLimit == 0`:

```solidity
if (providerInfo.defaultGasLimit == 0) {
    req.gasLimit10k = 0;  // legacy path selected
}
``` [3](#0-2) 

`clearRequest` is called **before** the external callback (checks-effects-interactions pattern), but because the callback is a bare direct call, any revert in `_entropyCallback` propagates upward and reverts the entire transaction — including the `clearRequest` effect. The request slot is restored to active, and the provider's revelation is discarded. [4](#0-3) 

The new flow correctly handles this with `ExcessivelySafeCall`:

```solidity
using ExcessivelySafeCall for address;
...
(success, ret) = req.requester.excessivelySafeCall(
    uint256(req.gasLimit10k) * TEN_THOUSAND,
    256,
    abi.encodeWithSelector(IEntropyConsumer._entropyCallback.selector, ...)
);
``` [5](#0-4) [6](#0-5) 

The legacy path has no equivalent protection.

---

### Impact Explanation

When a consumer contract's `_entropyCallback` reverts (due to a paused dependency, an internal invariant violation, or a deliberate revert), the provider's `revealWithCallback` call reverts entirely. Because there is no cancel/refund mechanism in the Entropy contract, the request is permanently stuck:

- The user already paid the full fee at request time (provider fee credited to `providerInfo.accruedFeesInWei`, Pyth fee to `accruedPythFeesInWei`).
- The user never receives the random number.
- The provider cannot clear the request.
- No recovery path exists for the user. [7](#0-6) 

This is a **permanent loss of service and effective loss of funds** for the user.

---

### Likelihood Explanation

The legacy path is active for any provider whose `defaultGasLimit` is `0` (the default for newly registered providers who have not called `setDefaultGasLimit`). Consumer contracts commonly call external protocols (token transfers, DEX interactions, oracle reads) inside `_entropyCallback`. Any of these dependencies being paused, rate-limited, or reverting causes the entire fulfillment to fail permanently. An unprivileged user only needs to deploy a consumer contract with such a dependency and request randomness from a legacy-path provider.

---

### Recommendation

Wrap the legacy-path callback in a try-catch (or use `excessivelySafeCall`) so that a reverting consumer does not block fulfillment:

```solidity
try IEntropyConsumer(callAddress)._entropyCallback(
    sequenceNumber, provider, randomNumber
) {
    // success
} catch {
    emit CallbackFailed(...);
}
```

Alternatively, migrate all providers to the new flow by requiring `defaultGasLimit > 0` on registration, and deprecate the legacy path entirely.

---

### Proof of Concept

1. Deploy a provider with `defaultGasLimit == 0` (default state after `register()`).
2. Deploy a malicious/buggy consumer contract whose `_entropyCallback` always reverts.
3. Consumer calls `requestWithCallback(provider, userContribution)` — pays the full fee; `req.gasLimit10k` is set to `0`.
4. Provider calls `revealWithCallback(provider, seq, userContribution, providerContribution)`.
5. Execution enters the legacy `else` branch; `clearRequest` runs, then `_entropyCallback` reverts.
6. The entire transaction reverts; `clearRequest` is undone; the request remains active.
7. No matter how many times the provider retries, the transaction always reverts.
8. The user's fee is permanently locked; the random number is never delivered. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L70-70)
```text
    using ExcessivelySafeCall for address;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L234-239)
```text
        uint128 requiredFee = getFeeV2(provider, callbackGasLimit);
        if (msg.value < requiredFee) revert EntropyErrors.InsufficientFee();
        uint128 providerFee = getProviderFee(provider, callbackGasLimit);
        providerInfo.accruedFeesInWei += providerFee;
        _state.accruedPythFeesInWei += (SafeCast.toUint128(msg.value) -
            providerFee);
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-272)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
        } else {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L542-560)
```text
    function revealWithCallback(
        address provider,
        uint64 sequenceNumber,
        bytes32 userContribution,
        bytes32 providerContribution
    ) public override {
        EntropyStructsV2.Request storage req = findActiveRequest(
            provider,
            sequenceNumber
        );

        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-578)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
            req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L579-596)
```text
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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-702)
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
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());

            emit RevealedWithCallback(
                reqV1,
                userContribution,
                providerContribution,
                randomNumber
            );
            emit EntropyEventsV2.Revealed(
                provider,
                callAddress,
                sequenceNumber,
                randomNumber,
                userContribution,
                providerContribution,
                false,
                bytes(""),
                gasUsed,
                bytes("")
            );
        }
```
