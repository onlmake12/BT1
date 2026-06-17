### Title
DoS via Unprotected External Callback in Legacy `revealWithCallback` Path — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

The `revealWithCallback` function in `Entropy.sol` contains two execution paths. The **new path** (when `req.gasLimit10k != 0`) uses `excessivelySafeCall` to catch consumer callback reverts and transitions the request to a recoverable `CALLBACK_FAILED` state. The **legacy path** (when `req.gasLimit10k == 0`) calls `IEntropyConsumer._entropyCallback` directly with no `try/catch` and no failure-state fallback. If the consumer's callback reverts for any reason, the entire `revealWithCallback` transaction reverts, the request is never cleared, and the user's fee remains permanently locked in the contract with no recovery path.

---

### Finding Description

In `Entropy.sol`, `revealWithCallback` branches on whether the request carries an explicit gas limit:

```solidity
if (
    req.gasLimit10k != 0 &&
    req.callbackStatus == EntropyStatusConstants.CALLBACK_NOT_STARTED
) {
    // NEW PATH — uses excessivelySafeCall, sets CALLBACK_FAILED on revert
    (success, ret) = req.requester.excessivelySafeCall(...);
    ...
    req.callbackStatus = EntropyStatusConstants.CALLBACK_FAILED; // recoverable
} else {
    // LEGACY PATH — direct call, no try/catch, no failure state
    clearRequest(provider, sequenceNumber);
    if (len != 0) {
        IEntropyConsumer(callAddress)._entropyCallback(   // ← bare external call
            sequenceNumber,
            provider,
            randomNumber
        );
    }
}
```

In the legacy path:
1. `clearRequest` is called before the external call (CEI pattern).
2. `_entropyCallback` is invoked with **no error handling**.
3. If the callback reverts for any reason (paused consumer, logic bug, out-of-gas, intentional revert), the **entire transaction reverts**, including the `clearRequest` side-effect.
4. The request is restored to active state, but the callback will revert again on every subsequent attempt.
5. There is **no `CALLBACK_FAILED` state** and **no refund mechanism** for this path, so the user's fee is permanently locked.

Requests enter the legacy path whenever `requestWithCallback` is called without specifying a gas limit (i.e., `gasLimit10k == 0`), which is the default for older integrations and any caller that omits the gas limit parameter.

---

### Impact Explanation

- **User funds locked**: The fee paid at request time is credited to the provider only upon a successful `revealWithCallback`. If the callback always reverts, the fee is never credited and cannot be withdrawn by either party.
- **Permanent DoS on the request**: Unlike the new path (which sets `CALLBACK_FAILED` and allows a retry), the legacy path has no recovery state. The request is stuck indefinitely.
- **Provider griefing**: A provider cannot collect fees for requests whose consumers revert, even if the provider behaves correctly.

---

### Likelihood Explanation

Any of the following realistic conditions trigger the issue:

- A consumer contract that is **pausable** (e.g., OpenZeppelin `Pausable`) and is paused at the time of reveal.
- A consumer contract that **runs out of gas** inside `_entropyCallback` (no gas limit was set, so the full remaining gas is forwarded, but the callback may still exhaust it).
- A consumer contract with a **logic bug** that reverts on certain random values (e.g., division by zero, array out-of-bounds).
- A consumer contract that **self-destructs** between request and reveal, causing the call to revert.

All of these are realistic for production integrations. The legacy path is still reachable by any caller that does not explicitly set a gas limit.

---

### Recommendation

Apply the same `excessivelySafeCall` + `CALLBACK_FAILED` state machine to the legacy path, or deprecate the legacy path entirely by requiring `gasLimit10k != 0` for all new requests. At minimum, wrap the bare `_entropyCallback` call in a `try/catch` and emit a failure event so the request can be retried or refunded.

---

### Proof of Concept

1. Deploy a consumer contract whose `_entropyCallback` always reverts (e.g., contains `require(false, "paused")`).
2. Call `requestWithCallback` **without** specifying a gas limit (`gasLimit10k == 0`). Pay the required fee.
3. Provider calls `revealWithCallback(provider, sequenceNumber, userContribution, providerContribution)`.
4. Transaction reverts because `_entropyCallback` reverts; `clearRequest` is rolled back.
5. Repeat step 3 — same result every time.
6. The fee paid in step 2 is permanently locked; neither the user nor the provider can recover it.

The divergence between the two paths is visible at: [1](#0-0) 

The unprotected legacy call is at: [2](#0-1) 

The new path's recoverable failure state (absent in the legacy path) is set at: [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-599)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L621-651)
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
