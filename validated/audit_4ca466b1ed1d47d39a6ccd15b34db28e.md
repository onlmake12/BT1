### Title
Reentrancy in `revealWithCallback` Allows Callback to Overwrite Request Ring-Buffer Slot, Causing Event/State Mismatch — (`File: target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

---

### Summary

In `revealWithCallback`'s new-flow path (when `req.gasLimit10k != 0` and `callbackStatus == CALLBACK_NOT_STARTED`), the `RevealedWithCallback` and `EntropyEventsV2.Revealed` events are emitted using **live storage reads from `req` after the external callback returns**, but before `clearRequest` is called. A malicious callback contract can call `requestV2` during the callback to create a new request that maps to the same ring-buffer slot (index `seqNum % NUM_REQUESTS`). This causes `allocRequest` to evict the original request to `requestsOverflow` and overwrite the slot with the new request's data. The subsequent event emission then reads the new request's `requester` and `sequenceNumber` from storage, emitting events that do not match the actual fulfilled request.

---

### Finding Description

`revealWithCallback` in `Entropy.sol` has two execution paths. The new-flow path (lines 574–660) is entered when `req.gasLimit10k != 0` and `callbackStatus == CALLBACK_NOT_STARTED`:

```solidity
req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;   // line 578
(success, ret) = req.requester.excessivelySafeCall(                  // line 582
    uint256(req.gasLimit10k) * TEN_THOUSAND,
    256,
    abi.encodeWithSelector(IEntropyConsumer._entropyCallback.selector, ...)
);
req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;   // line 599

if (success) {
    emit RevealedWithCallback(
        EntropyStructConverter.toV1Request(req),   // reads storage AFTER external call
        ...
    );
    emit EntropyEventsV2.Revealed(
        provider,
        req.requester,       // reads storage AFTER external call
        req.sequenceNumber,  // reads storage AFTER external call
        ...
    );
    clearRequest(provider, sequenceNumber);        // line 620
}
``` [1](#0-0) 

The request ring-buffer has `NUM_REQUESTS = 32` slots. A request with sequence number `S` occupies slot `S % 32`. [2](#0-1) 

When `allocRequest` is called for a new sequence number `S'` where `S' % 32 == S % 32`, the comment in `EntropyState.sol` confirms:

> *"If that slot in the array is already occupied by a prior request, the prior request is evicted into the `requestsOverflow` mapping."* [3](#0-2) 

`req` is a `storage` pointer returned by `findActiveRequest` pointing to `_state.requests[S % 32]`. After the callback evicts `S` to overflow and writes `S'` into the slot, `req` now reads `S'`'s fields. The events at lines 602–619 therefore emit `S'`'s `requester` and `sequenceNumber` instead of `S`'s. The `randomNumber` value itself is correct (computed from `S`'s commitment before the external call at line 562), but the identity fields in the event are wrong. [4](#0-3) 

The `CALLBACK_IN_PROGRESS` guard at line 553–558 only blocks re-entry into `revealWithCallback` for the same sequence number. It does **not** block calls to `requestV2`, which is the reentrant path used here. [5](#0-4) 

---

### Impact Explanation

- `RevealedWithCallback` and `EntropyEventsV2.Revealed` events are emitted with the wrong `requester` and `sequenceNumber` (those of the newly created request `S'` instead of the fulfilled request `S`).
- Light clients and off-chain indexers that rely on these events (rather than direct state reads) will record an incorrect fulfillment: they will believe `S'` was revealed when it was not, and will not know `S` was revealed.
- The new request `S'` remains active in the slot (not cleared), so the attacker retains a live pending request they can fulfill later.
- `clearRequest(provider, S)` correctly clears `S` from overflow, so the original request is cleaned up — but the emitted event data is permanently wrong on-chain.

---

### Likelihood Explanation

- The attacker must be the `requester` (a contract implementing `_entropyCallback`), which is the normal use case for `requestWithCallback`/`requestV2`.
- The attacker sets a high `callbackGasLimit` when making the original request to give the callback enough gas to make up to 32 `requestV2` calls.
- With `NUM_REQUESTS = 32`, at most 32 sequential `requestV2` calls are needed to find a sequence number `S'` such that `S' % 32 == S % 32`.
- The callback contract must be pre-funded with ETH to pay fees for those calls — straightforward for a motivated attacker.
- No privileged role is required; any user of the Entropy protocol can be the requester.

---

### Recommendation

Snapshot `req.requester` and `req.sequenceNumber` into **memory variables before** the `excessivelySafeCall`, and use those memory variables for event emission. This mirrors the pattern already used in the else-branch (old flow) at lines 663–665, where `reqV1` is captured as a memory copy before `clearRequest` and the external call:

```solidity
// Capture before external call
address requester = req.requester;
uint64 seqNum = req.sequenceNumber;
EntropyStructs.Request memory reqV1 = EntropyStructConverter.toV1Request(req);

req.callbackStatus = EntropyStatusConstants.CALLBACK_IN_PROGRESS;
(success, ret) = req.requester.excessivelySafeCall(...);
req.callbackStatus = EntropyStatusConstants.CALLBACK_NOT_STARTED;

if (success) {
    emit RevealedWithCallback(reqV1, ...);           // use memory copy
    emit EntropyEventsV2.Revealed(provider, requester, seqNum, ...);  // use memory copy
    clearRequest(provider, sequenceNumber);
}
``` [6](#0-5) 

---

### Proof of Concept

1. Attacker deploys `MaliciousConsumer` contract, pre-funded with ETH for fees.
2. Attacker calls `requestV2(provider, userCommitment, highGasLimit)` — assigned sequence number `S`. Suppose `S % 32 == 7`.
3. Provider calls `revealWithCallback(provider, S, userContribution, providerContribution)`.
4. `findActiveRequest` returns a storage pointer `req` → `_state.requests[7]` (containing `S`'s data).
5. `revealHelper` computes `randomNumber` from `S`'s commitment (correct).
6. `req.callbackStatus = CALLBACK_IN_PROGRESS`.
7. `excessivelySafeCall` invokes `MaliciousConsumer._entropyCallback(S, provider, randomNumber)`.
8. Inside the callback, `MaliciousConsumer` calls `requestV2` in a loop until it receives sequence number `S'` where `S' % 32 == 7`. With `NUM_REQUESTS = 32`, this takes at most 32 iterations.
9. `allocRequest(provider, S')` evicts `S` from slot 7 to `requestsOverflow`, writes `S'` into slot 7.
10. Callback returns normally → `success = true`.
11. `req.callbackStatus = CALLBACK_NOT_STARTED` (writes to slot 7, now `S'`'s field — no-op since it's already `CALLBACK_NOT_STARTED`).
12. `emit RevealedWithCallback(EntropyStructConverter.toV1Request(req), ...)` — emits `S'`'s data.
13. `emit EntropyEventsV2.Revealed(provider, req.requester, req.sequenceNumber, ...)` — emits `S'`'s `requester` and `S'` as the sequence number, paired with the random number derived from `S`'s commitment.
14. `clearRequest(provider, S)` — finds `S` in overflow, clears it. Slot 7 (containing `S'`) is untouched.
15. Result: on-chain events claim `S'` was revealed; `S'` remains active and can be fulfilled again; `S`'s reveal is permanently misattributed in the event log.

### Citations

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L553-559)
```text
        if (
            !(req.callbackStatus ==
                EntropyStatusConstants.CALLBACK_NOT_STARTED ||
                req.callbackStatus == EntropyStatusConstants.CALLBACK_FAILED)
        ) {
            revert EntropyErrors.InvalidRevealCall();
        }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L561-566)
```text
        bytes32 randomNumber;
        (randomNumber, ) = revealHelper(
            req,
            userContribution,
            providerContribution
        );
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L574-620)
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
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L661-667)
```text
        } else {
            // This case uses the checks-effects-interactions pattern to avoid reentry attacks
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
```

**File:** target_chains/ethereum/contracts/contracts/entropy/EntropyState.sol (L27-48)
```text
        // This data structure is a two-level hash table. It first tries to store new requests in the requests array at
        // an index determined by a few bits of the request's key. If that slot in the array is already occupied by a
        // prior request, the prior request is evicted into the requestsOverflow mapping. Requests in the array are
        // considered active if their sequenceNumber is > 0.
        //
        // WARNING: the number of requests must be kept in sync with the constants below
        EntropyStructsV2.Request[32] requests;
        mapping(bytes32 => EntropyStructsV2.Request) requestsOverflow;
        // Mapping from randomness providers to information about each them.
        mapping(address => EntropyStructsV2.ProviderInfo) providers;
        // proposedAdmin is the new admin's account address proposed by either the owner or the current admin.
        // If there is no pending transfer request, this value will hold `address(0)`.
        address proposedAdmin;
        // Seed for in-contract PRNG. This seed is used to generate user random numbers in some callback flows.
        bytes32 seed;
    }
}

contract EntropyState {
    // The size of the requests hash table. Must be a power of 2.
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```
