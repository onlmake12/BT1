### Title
Malicious Requester Can Permanently Block `revealWithCallback` Fulfillment via Reverting Callback — (File: `target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `Entropy.sol`, the `revealWithCallback` function contains a legacy execution path (the "old path") that makes a direct, unchecked call to the requester's `_entropyCallback`. If the requester is a contract that deliberately reverts in its callback, the entire `revealWithCallback` transaction reverts — including the `clearRequest` side-effect — leaving the request permanently stuck and the user's fee locked in the contract forever.

---

### Finding Description

`revealWithCallback` branches on whether `req.gasLimit10k != 0`:

- **New path** (`gasLimit10k != 0`): uses `excessivelySafeCall` which catches reverts and transitions the request to `CALLBACK_FAILED`, allowing recovery.
- **Old path** (`gasLimit10k == 0`): makes a bare, unchecked call to `IEntropyConsumer(callAddress)._entropyCallback(...)`. [1](#0-0) 

The old path is reached whenever `req.gasLimit10k == 0`. This happens when the provider has not configured a `defaultGasLimit` (i.e., `providerInfo.defaultGasLimit == 0`), which is the default state for any newly registered provider. [2](#0-1) 

In the old path, `clearRequest` is called **before** the callback: [3](#0-2) 

If `_entropyCallback` reverts, the EVM unwinds the entire transaction — including the `clearRequest` write — so the request remains active. The provider cannot fulfill it, cannot skip it, and there is no cancellation mechanism. The user's fee is permanently locked.

---

### Impact Explanation

- The request is permanently stuck: `revealWithCallback` will always revert for that sequence number.
- The user's fee (paid at request time) is locked in the contract with no recovery path.
- The provider wastes gas on every fulfillment attempt.
- Any provider that has not set `defaultGasLimit` (the default state) is vulnerable to this for all their requests.

---

### Likelihood Explanation

Any unprivileged Entropy user can deploy a contract whose `_entropyCallback` unconditionally reverts, then call `requestV2` (or the legacy `request`) through that contract. The provider is then unable to fulfill the request. This requires no special access, no governance majority, and no privileged key — only a small fee payment to submit the request.

---

### Recommendation

In the old path, wrap the direct callback in a `try/catch` (or use `excessivelySafeCall`) and transition the request to a failed/clearable state on revert, consistent with the new path's behavior. Alternatively, require all providers to set a non-zero `defaultGasLimit` before accepting requests, so all requests use the safe new path.

---

### Proof of Concept

1. Deploy `MaliciousConsumer` implementing `IEntropyConsumer` with `_entropyCallback` that always reverts.
2. Call `entropy.requestV2{value: fee}(provider, userRandom, 0)` from `MaliciousConsumer` (or use a provider with `defaultGasLimit == 0` so `gasLimit10k` is stored as 0).
3. Provider calls `entropy.revealWithCallback(provider, seqNum, userContrib, providerContrib)`.
4. Execution reaches the old path at line 663; `clearRequest` executes, then `_entropyCallback` reverts.
5. Entire transaction reverts; request is still active.
6. Step 3 can be repeated indefinitely — it always reverts. The user's fee is permanently locked. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L268-271)
```text
        if (providerInfo.defaultGasLimit == 0) {
            // Provider doesn't support the new callback failure state flow (toggled by setting the gas limit field).
            // Set gasLimit10k to 0 to disable.
            req.gasLimit10k = 0;
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-596)
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
