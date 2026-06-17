### Title
No Contract Existence Check in `revealWithCallback` gasLimit Path Silently Swallows Callbacks to EOAs or Self-Destructed Contracts — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

`Entropy.revealWithCallback` has two execution paths. The legacy path (no gasLimit) correctly guards with an `extcodesize` check before invoking the callback. The newer gasLimit path omits this check entirely. When `req.requester` has no code (EOA or self-destructed contract), `excessivelySafeCall` returns `success = true` because EVM calls to codeless addresses always succeed. The contract then emits `Revealed` with `callbackFailed = false`, emits `RevealedWithCallback`, and permanently clears the request — all while the callback was never executed.

---

### Finding Description

`Entropy.sol` uses two distinct paths inside `revealWithCallback`:

**Path 2 — legacy (no gasLimit), lines 661–702:** correctly checks `extcodesize` before invoking the callback.

```solidity
uint len;
assembly {
    len := extcodesize(callAddress)
}
if (len != 0) {
    IEntropyConsumer(callAddress)._entropyCallback(...);
}
``` [1](#0-0) 

**Path 1 — gasLimit path, lines 574–660:** no `extcodesize` check. It calls `excessivelySafeCall` directly on `req.requester`:

```solidity
(success, ret) = req.requester.excessivelySafeCall(
    uint256(req.gasLimit10k) * TEN_THOUSAND,
    256,
    abi.encodeWithSelector(IEntropyConsumer._entropyCallback.selector, ...)
);
``` [2](#0-1) 

In the EVM, a `CALL` to an address with no deployed bytecode (EOA or self-destructed contract) always returns `success = true` with empty return data. `excessivelySafeCall` is a wrapper around a low-level `call` and inherits this behavior. Therefore, when `req.requester` has no code, `success` is `true`, and the contract takes the success branch:

```solidity
if (success) {
    emit RevealedWithCallback(...);
    emit EntropyEventsV2.Revealed(..., false, ret, ...); // callbackFailed = false
    clearRequest(provider, sequenceNumber);
}
``` [3](#0-2) 

The request is permanently cleared. The callback was never delivered. The system reports success.

By contrast, `Executor.sol` correctly guards its low-level call with an `extcodesize` check and reverts on `len == 0`:

```solidity
assembly { len := extcodesize(callAddress) }
if (len == 0) revert ExecutorErrors.InvalidContractTarget();
``` [4](#0-3) 

The gasLimit path in `Entropy.sol` lacks this guard entirely.

---

### Impact Explanation

- The Entropy request is permanently cleared via `clearRequest` — no recovery path exists for the requester.
- The fee paid by the requester is consumed and credited to the provider.
- Downstream systems and off-chain monitors observing the `Revealed(callbackFailed=false)` event are misled into believing the callback was successfully delivered.
- The requester's application logic (e.g., a game, lottery, or DeFi protocol) never receives the random number, causing silent failure of dependent state transitions. [3](#0-2) 

---

### Likelihood Explanation

Two realistic trigger scenarios exist for an unprivileged actor:

1. **Self-destruct after request:** A contract requests randomness with a gasLimit, then self-destructs (e.g., via a CREATE2 redeploy pattern or intentional teardown) before the provider calls `revealWithCallback`. The provider's call succeeds silently.

2. **EOA requests with gasLimit:** If the `request`/`requestV2` entry point does not enforce that `msg.sender` is a contract when `gasLimit > 0`, an EOA can create a request with `gasLimit10k > 0`. When `revealWithCallback` is called, the gasLimit path is taken, `excessivelySafeCall` to the EOA returns `success = true`, and the request is cleared as if the callback succeeded.

Neither scenario requires privileged access, leaked keys, or oracle manipulation. [5](#0-4) 

---

### Recommendation

Add an `extcodesize` check at the start of the gasLimit path, mirroring the guard already present in the legacy path and in `Executor.sol`:

```solidity
uint len;
assembly { len := extcodesize(req.requester) }
if (len == 0) {
    // Requester has no code; treat as a failed callback or revert
    revert EntropyErrors.InvalidCallbackAddress();
}
```

Alternatively, enforce at request time that `msg.sender` must have deployed code when `gasLimit > 0`. [6](#0-5) 

---

### Proof of Concept

1. Deploy a contract `Requester` that implements `IEntropyConsumer._entropyCallback`.
2. `Requester` calls `entropy.request(provider, userCommitment, true)` with `gasLimit > 0`, paying the required fee. Record `sequenceNumber`.
3. `Requester` calls `selfdestruct(address(0))`. `Requester` now has no code.
4. Anyone calls `entropy.revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
5. Inside `revealWithCallback`, `req.gasLimit10k != 0` → gasLimit path is taken.
6. `req.requester.excessivelySafeCall(...)` is called on the now-codeless address → returns `(true, "")`.
7. `success == true` → `RevealedWithCallback` and `Revealed(callbackFailed=false)` are emitted; `clearRequest` is called.
8. Observe: callback was never executed; request is permanently gone; fee is consumed; events report success. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-660)
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
            } else {
                // Callback reverted by (potentially) running out of gas, but the calling context did not have enough gas
                // to run the callback. This is a corner case that can happen due to the nuances of gas passing
                // in calls (see the comment on the call above).
                //
                // (Note that reverting here plays nicely with the estimateGas RPC method, which binary searches for
                // the smallest gas value that causes the transaction to *succeed*. See https://github.com/ethereum/go-ethereum/pull/3587 )
                revert EntropyErrors.InsufficientGas();
            }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L669-681)
```text
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

**File:** target_chains/ethereum/contracts/contracts/executor/Executor.sol (L84-89)
```text
        uint len;
        address callAddress = address(gi.callAddress);
        assembly {
            len := extcodesize(callAddress)
        }
        if (len == 0) revert ExecutorErrors.InvalidContractTarget();
```
