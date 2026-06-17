### Title
`excessivelySafeCall` on `req.requester` Lacks Contract Existence Check, Silently Consuming Entropy Requests for EOA/Destroyed-Contract Requesters — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`'s `revealWithCallback`, the new V2 callback path (triggered when `req.gasLimit10k != 0`) calls `req.requester.excessivelySafeCall(...)` without first verifying that `req.requester` is a contract. Because `excessivelySafeCall` is a low-level call wrapper, it returns `(true, "")` when the target is an EOA or a self-destructed contract — exactly the EVM behavior warned about in the Solidity documentation. The code then interprets this as a successful callback, clears the request, and emits `RevealedWithCallback`, permanently consuming the random number with no recovery path.

The old (V1) path in the same function explicitly guards against this with an `extcodesize` check. The new path omits it, creating an inconsistency and a silent failure mode.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` has two execution branches depending on `req.gasLimit10k`:

**New path (gasLimit10k != 0) — no existence check:**

```solidity
(success, ret) = req.requester.excessivelySafeCall(
    uint256(req.gasLimit10k) * TEN_THOUSAND,
    256,
    abi.encodeWithSelector(
        IEntropyConsumer._entropyCallback.selector,
        sequenceNumber, provider, randomNumber
    )
);
```

If `req.requester` is an EOA or a self-destructed contract, the EVM CALL opcode returns `success = true` with empty return data. The code then enters the `if (success)` branch:

```solidity
if (success) {
    emit RevealedWithCallback(...);
    emit EntropyEventsV2.Revealed(...);
    clearRequest(provider, sequenceNumber);
}
```

The request is permanently cleared. No `CallbackFailed` event is emitted. No `CALLBACK_FAILED` state is set. There is no retry path.

**Old path (gasLimit10k == 0) — has existence check:**

```solidity
uint len;
assembly {
    len := extcodesize(callAddress)
}
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(
        sequenceNumber, provider, randomNumber
    );
}
```

The old path correctly skips the callback for EOAs/non-contracts. The new path omits this guard entirely. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

When `req.gasLimit10k != 0` and `req.requester` has no code (EOA or self-destructed contract):

1. `excessivelySafeCall` returns `(true, "")` — no code was executed.
2. The `if (success)` branch fires: `RevealedWithCallback` is emitted and `clearRequest` is called.
3. The request slot is permanently freed. The random number is consumed.
4. The `CALLBACK_FAILED` state is never set, so the user has **no recovery path** — unlike a legitimately failing contract callback, which would set `callbackStatus = CALLBACK_FAILED` and allow a retry.

The user paid the full fee (including the provider's gas-limit-scaled fee) for a random number they will never receive, with no mechanism to recover it. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

The trigger conditions are:

- A user calls `requestV2` / `requestWithCallback` from an EOA address (possible if a script or wallet directly calls the function without a consumer contract), **or**
- A user's consumer contract is self-destructed between the request and the reveal transaction.

The first scenario is realistic: developers testing integrations, scripts, or wallets can accidentally call `requestWithCallback` directly from an EOA. The V2 API (`requestV2`) is designed for contracts, but there is no on-chain enforcement preventing an EOA from calling it. The `req.requester = msg.sender` assignment stores whatever address called the function. [5](#0-4) 

---

### Recommendation

Before the `excessivelySafeCall` invocation in the new path, add the same `extcodesize` guard used in the old path:

```solidity
uint len;
assembly { len := extcodesize(req.requester) }
if (len == 0) {
    // Treat as a failed callback so the user can recover or be informed
    emit CallbackFailed(provider, req.requester, sequenceNumber, ...);
    req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED;
    return;
}
```

Alternatively, unify both paths to always check `extcodesize` before any callback attempt, consistent with the documented EVM behavior. [6](#0-5) 

---

### Proof of Concept

1. Deploy no consumer contract. Call `requestV2(provider, gasLimit)` directly from an EOA wallet, paying the required fee. `req.requester = msg.sender` (the EOA). `req.gasLimit10k != 0`.
2. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
3. The new path executes: `req.requester.excessivelySafeCall(gasLimit, 256, calldata)`.
4. The EVM CALL to the EOA returns `(true, "")`.
5. `if (success)` is true → `RevealedWithCallback` is emitted → `clearRequest` is called.
6. The random number is consumed. No `_entropyCallback` was ever executed. No `CallbackFailed` event was emitted. The request cannot be retried. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L258-261)
```text
            bytes.concat(userCommitment, providerInfo.currentCommitment)
        );
        req.requester = msg.sender;

```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-651)
```text
        if (
            req.gasLimit10k != 0 &&
            req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
        ) {
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
            uint32 gasUsed = SafeCast.toUint32(startingGas - gasleft());
            // Reset status to not started here in case the transaction reverts.
            req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;

            if (success) {
                emit RevealedWithCallback(
                    EntropyStructConverter.toV1Request(req),
                    userContribution,
                    providerContribution,
                    randomNumber
                );
                emit EntropyEventsV2.Revealed(
                    provider,
                    req.requester,
                    req.sequenceNumber,
                    randomNumber,
                    userContribution,
                    providerContribution,
                    false,
                    ret,
                    SafeCast.toUint32(gasUsed),
                    bytes("")
                );
                clearRequest(provider, sequenceNumber);
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L662-681)
```text
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
