### Title
Silent Callback Skip for Overflow Requests via Use-After-Clear of Storage Reference — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.executeCallback`, a `storage` reference `req` is obtained via `findActiveRequest`, then `clearRequest` is called. For requests stored in the overflow mapping, `clearRequest` executes `delete _state.requestsOverflow[key]`, which zeroes the entire struct in storage. The subsequent read of `req.requester` and `req.callbackGasLimit` from the now-zeroed storage causes the consumer callback to be silently invoked on `address(0)` with 0 gas — permanently skipping the consumer's price update callback while the provider is credited and the request is marked fulfilled.

---

### Finding Description

`Echo.executeCallback` follows this sequence:

1. Obtain a `storage` reference to the active request.
2. Credit the provider's `accruedFeesInWei`.
3. Call `clearRequest(sequenceNumber)`.
4. Update `_state.firstUnfulfilledSeq`.
5. Call `IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)`. [1](#0-0) 

`findActiveRequest` returns a `storage` reference. For requests in the overflow mapping, `findRequest` returns `_state.requestsOverflow[key]`: [2](#0-1) 

`clearRequest` for overflow requests executes `delete _state.requestsOverflow[key]`, which zeroes the entire struct at that storage location: [3](#0-2) 

After the `delete`, the `req` storage pointer still points to the same (now-zeroed) location. Reading `req.requester` returns `address(0)` and `req.callbackGasLimit` returns `0`: [4](#0-3) 

A call to `IEchoConsumer(address(0))._echoCallback{gas: 0}(...)` succeeds silently (no code at `address(0)`), causing the `try` block to succeed and `emitPriceUpdate` to emit a misleading `PriceUpdateExecuted` event — while the actual consumer contract never receives the callback.

Requests end up in the overflow mapping via `allocRequest`, which displaces an existing active request to overflow whenever a new request hashes to the same slot: [5](#0-4) 

With 32 slots (`NUM_REQUESTS`) and sequential sequence numbers, collisions are expected under normal usage (birthday paradox: ~50% probability after ~7 concurrent requests).

---

### Impact Explanation

Any consumer whose request is displaced to the overflow mapping permanently loses their price update callback. The provider is still credited the full fee, and the request is marked as fulfilled. The consumer's application logic that depends on the price update (e.g., executing a trade, updating a position) never executes. This constitutes a loss of service and potential financial loss for the consumer, with no recourse since the request is cleared.

---

### Likelihood Explanation

With 32 slots and sequential sequence numbers assigned by the contract, collisions occur naturally under normal usage — no adversarial action is required. An attacker can also deliberately trigger this by observing active requests (via emitted events) and submitting a request timed to receive a sequence number that collides with an existing active request, since sequence numbers are sequential and the hash function is deterministic and public.

---

### Recommendation

Save `req.requester` and `req.callbackGasLimit` to local memory variables **before** calling `clearRequest`:

```solidity
address callbackTarget = req.requester;
uint32 callbackGas = req.callbackGasLimit;

clearRequest(sequenceNumber);

// ... update firstUnfulfilledSeq ...

try IEchoConsumer(callbackTarget)._echoCallback{gas: callbackGas}(sequenceNumber, priceFeeds) {
    ...
}
```

This mirrors the pattern already used in `Entropy.revealWithCallback` (the `else` branch), which explicitly saves `callAddress` before `clearRequest`: [6](#0-5) 

---

### Proof of Concept

1. Request A is created with sequence number N; `shortKey(N) = S`; stored in `_state.requests[S]`.
2. Request B is created with sequence number M where `shortKey(M) = S`; `allocRequest` moves Request A to `_state.requestsOverflow[key(N)]` and stores Request B in `_state.requests[S]`.
3. Provider calls `executeCallback` for Request A (sequence number N).
4. `findActiveRequest(N)` returns `_state.requestsOverflow[key(N)]` as a `storage` reference `req`.
5. Provider fees are credited: `_state.providers[providerToCredit].accruedFeesInWei += ...`.
6. `clearRequest(N)` executes `delete _state.requestsOverflow[key(N)]` — entire struct zeroed.
7. `req.requester` is now `address(0)`; `req.callbackGasLimit` is now `0`.
8. `IEchoConsumer(address(0))._echoCallback{gas: 0}(N, priceFeeds)` is called.
9. Call succeeds (no code at `address(0)`); `PriceUpdateExecuted` is emitted.
10. Consumer A's contract never receives the callback; their application logic is permanently skipped.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L111-111)
```text
        Request storage req = findActiveRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-201)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
        {
            // Callback succeeded
            emitPriceUpdate(sequenceNumber, priceIds, priceFeeds);
        } catch Error(string memory reason) {
            // Explicit revert/require
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                reason
            );
        } catch {
            // Out of gas or other low-level errors
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                "low-level error (possibly out of gas)"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L310-321)
```text
    function findRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            return req;
        } else {
            req = _state.requestsOverflow[key];
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L323-332)
```text
    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L334-344)
```text
    function allocRequest(
        uint64 sequenceNumber
    ) internal returns (Request storage req) {
        (, uint8 shortKey) = requestKey(sequenceNumber);

        req = _state.requests[shortKey];
        if (isActive(req)) {
            (bytes32 reqKey, ) = requestKey(req.sequenceNumber);
            _state.requestsOverflow[reqKey] = req;
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/entropy/Entropy.sol (L663-667)
```text
            address callAddress = req.requester;
            EntropyStructs.Request memory reqV1 = EntropyStructConverter
                .toV1Request(req);
            clearRequest(provider, sequenceNumber);
            // WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
```
