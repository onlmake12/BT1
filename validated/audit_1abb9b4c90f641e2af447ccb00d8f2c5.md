### Title
Reentrancy Guard Bypass via Storage Pointer Aliasing in `revealWithCallback` — (`target_chains/ethereum/contracts/contracts/entropy/Entropy.sol`)

### Summary

In `Entropy.revealWithCallback`, the `req` storage pointer is obtained before the external callback is made. During the callback, a malicious requester contract can create a new request that maps to the same storage slot, causing `allocRequest` to move the in-flight request to the overflow mapping and overwrite the slot. After the callback returns, all post-call state writes (including the reentrancy guard reset and the `CALLBACK_FAILED` assignment) operate on the **new** request's storage, not the original one. This leaves the original request permanently stuck with `CALLBACK_IN_PROGRESS` status in the overflow mapping, making it permanently unfulfillable.

### Finding Description

`revealWithCallback` uses `CALLBACK_IN_PROGRESS` as a reentrancy guard: [1](#0-0) 

The flow in `revealWithCallback`:

1. **Line 578**: `req.callbackStatus = CALLBACK_IN_PROGRESS` — guard set on `_state.requests[shortKey]`
2. **Line 582**: `req.requester.excessivelySafeCall(...)` — external call to attacker-controlled contract
3. **During the call**: attacker's `_entropyCallback` calls `requestV2()` for a new request B that hashes to the same `shortKey`. `allocRequest` copies Request A (with `CALLBACK_IN_PROGRESS`) into `_state.requestsOverflow[key_A]` and overwrites `_state.requests[shortKey]` with Request B's data (`CALLBACK_NOT_STARTED`). The `req` storage pointer still points to `_state.requests[shortKey]`, which now holds Request B.
4. **Line 599**: `req.callbackStatus = CALLBACK_NOT_STARTED` — resets **Request B's** status (no-op), not Request A's
5. **If callback fails → Line 651**: `req.callbackStatus = CALLBACK_FAILED` — sets **Request B's** status to `CALLBACK_FAILED` (incorrect); Request A remains in overflow with `CALLBACK_IN_PROGRESS` [2](#0-1) 

The guard check at the top of `revealWithCallback` only allows `CALLBACK_NOT_STARTED` or `CALLBACK_FAILED`: [3](#0-2) 

Since Request A is now in overflow with `CALLBACK_IN_PROGRESS`, any future call to `revealWithCallback` for Request A will revert with `InvalidRevealCall`. The request is permanently unfulfillable.

The storage slot collision is deterministic: `shortKey = uint8(keccak256(provider, sequenceNumber)[0] & NUM_REQUESTS_MASK)`. With only 256 slots and sequentially assigned sequence numbers, an attacker can predict which future sequence number maps to the same slot as their current in-flight request. [4](#0-3) 

### Impact Explanation

- **Request A is permanently stuck**: `findActiveRequest` finds it in overflow with `callbackStatus == CALLBACK_IN_PROGRESS`, which fails the guard check and reverts. The request can never be fulfilled.
- **Fees lost**: The user paid fees for Request A. `revealHelper` already advanced the provider's commitment (consuming the randomness), but the callback is never successfully delivered.
- **State corruption**: In the success case, `RevealedWithCallback` and `Revealed` events are emitted with Request B's `requester` and `sequenceNumber` instead of Request A's, corrupting off-chain indexing.
- **Request B incorrectly marked `CALLBACK_FAILED`**: Request B, which was just created and never revealed, gets `CALLBACK_FAILED` status, allowing it to enter the recovery flow prematurely. [5](#0-4) 

### Likelihood Explanation

The attacker must be the requester (they control the callback contract). Any unprivileged user can deploy a malicious `IEntropyConsumer` contract. The attacker needs to predict the sequence number that will collide with their in-flight request's storage slot — this is fully deterministic and computable off-chain before making the request. The attack requires the provider to have `defaultGasLimit != 0` (the `gasLimit10k != 0` path), which is the intended production configuration for the new callback failure state flow.

### Recommendation

Do not use a raw storage pointer (`req`) across an external call boundary. Instead, either:
1. Copy all needed fields from `req` into memory variables before the external call, and use those memory variables for post-call state updates.
2. Re-fetch the request from storage after the external call using `findActiveRequest(provider, sequenceNumber)` to ensure the pointer is valid.
3. Clear the request from its primary storage slot before making the external call (checks-effects-interactions), similar to the `else` branch at line 662–666. [6](#0-5) 

### Proof of Concept

```solidity
contract MaliciousConsumer is IEntropyConsumer {
    IEntr

### Citations

**File:** target_chains/ethereum/entropy_sdk/solidity/EntropyStatusConstants.sol (L9-10)
```text
    // A request with callback where the callback is currently in flight (this state is a reentry guard).
    uint8 public constant CALLBACK_IN_PROGRESS = 2;
```

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

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L1048-1068)
```text
    function allocRequest(
        address provider,
        uint64 sequenceNumber
    ) internal returns (EntropyStructsV2.Request storage req) {
        (, uint8 shortKey) = requestKey(provider, sequenceNumber);

        req = _state.requests[shortKey];
        if (isActive(req)) {
            // There's already a prior active request in the storage slot we want to use.
            // Overflow the prior request to the requestsOverflow mapping.
            // It is important that this code overflows the *prior* request to the mapping, and not the new request.
            // There is a chance that some requests never get revealed and remain active forever. We do not want such
            // requests to fill up all of the space in the array and cause all new requests to incur the higher gas cost
            // of the mapping.
            //
            // This operation is expensive, but should be rare. If overflow happens frequently, increase
            // the size of the requests array to support more concurrent active requests.
            (bytes32 reqKey, ) = requestKey(req.provider, req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
    }
```
