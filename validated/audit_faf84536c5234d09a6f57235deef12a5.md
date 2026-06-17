### Title
Unprotected `_entropyCallback` in Legacy `revealWithCallback` Path Permanently Locks User Requests - (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function has two execution paths. The **new path** (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` with proper error handling and a `CALLBACK_FAILED` recovery state. The **legacy path** (when `req.gasLimit10k == 0`, i.e., requests made via `requestWithCallback` when the provider's `defaultGasLimit` is 0) calls `_entropyCallback` directly with **no try/catch and no recovery state**. If the consumer contract reverts, the entire `revealWithCallback` transaction reverts, the request remains permanently active, and no recovery mechanism exists for the user.

---

### Finding Description

In `Entropy.sol`, `revealWithCallback` branches on `req.gasLimit10k`:

```solidity
if (
    req.gasLimit10k != 0 &&
    req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
) {
    // NEW PATH: excessivelySafeCall + CALLBACK_FAILED state
    ...
} else {
    // LEGACY PATH: no error handling
    address callAddress = req.requester;
    clearRequest(provider, sequenceNumber);   // cleared before callback
    ...
    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(  // no try/catch
            sequenceNumber,
            provider,
            randomNumber
        );
    }
}
```

The legacy `else` branch is entered for any request where `gasLimit10k == 0`. This occurs when a provider has not called `setDefaultGasLimit` (leaving `defaultGasLimit = 0`) and a user calls `requestWithCallback`. In this path:

1. `clearRequest` is called **before** the external callback.
2. `_entropyCallback` is called with **no try/catch**.
3. If the consumer reverts, the entire transaction reverts — including the `clearRequest` — so the request remains active.
4. There is **no `CALLBACK_FAILED` state** for legacy requests, so there is no recovery path.
5. The provider cannot fulfill the request; every attempt reverts if the consumer always reverts.

The new path (lines 574–660) correctly uses `excessivelySafeCall` and transitions to `CALLBACK_FAILED`, allowing recovery. The legacy path has no equivalent protection.

---

### Impact Explanation

- **Permanent DoS on user's random number request**: A user who paid for entropy via `requestWithCallback` (legacy) against a provider with `defaultGasLimit = 0` will never receive their callback if their consumer contract reverts. There is no cancel or refund function in the Entropy contract.
- **Provider griefing**: A malicious user can deploy a consumer contract that always reverts, forcing the provider to waste gas on every fulfillment attempt indefinitely.
- **Funds locked**: The user's fee is already distributed at request time, but the service (random number delivery) is permanently denied with no recourse.

---

### Likelihood Explanation

- Any provider that has not explicitly called `setDefaultGasLimit` has `defaultGasLimit = 0`, making all their `requestWithCallback` users subject to this path.
- A consumer contract with a bug in `_entropyCallback` (e.g., an assertion that fails, an out-of-gas condition, or a dependency that becomes unavailable) will permanently brick that request.
- An unprivileged user can trigger this by simply calling `requestWithCallback` and deploying a consumer that reverts — no privileged access required.

---

### Recommendation

Wrap the legacy callback in a try/catch (or use `excessivelySafeCall`) and introduce a `CALLBACK_FAILED` state for legacy requests, mirroring the new path. Alternatively, migrate all requests to require an explicit gas limit so the legacy path is unreachable.

---

### Proof of Concept

1. Provider has `defaultGasLimit = 0` (never called `setDefaultGasLimit`).
2. User deploys a consumer contract whose `_entropyCallback` always reverts.
3. User calls `requestWithCallback(provider, userRandomNumber)` — request is created with `gasLimit10k = 0`.
4. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. Execution enters the legacy `else` branch at line 661.
6. `clearRequest` executes at line 666.
7. `_entropyCallback` is called at line 676 — consumer reverts.
8. Entire transaction reverts; `clearRequest` is rolled back; request remains active.
9. No `CALLBACK_FAILED` state exists for this request; no recovery path exists.
10. Provider retries → always reverts. User's request is permanently stuck.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-577)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L582-596)
```text
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
