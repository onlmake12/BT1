### Title
Echo `executeCallback` Calls Callback on `address(0)` After Overflow-Entry Deletion, Permanently Locking User Funds — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo.executeCallback` obtains a storage reference `req` to the active request, credits the provider fee, then calls `clearRequest`. When the request resides in the `requestsOverflow` mapping (evicted from the 32-slot main array due to a hash-table shortKey collision), `clearRequest` executes `delete _state.requestsOverflow[key]`, zeroing every field of the struct in place. The storage reference `req` still points to that zeroed slot, so the subsequent callback is dispatched to `address(0)` with 0 gas, succeeds silently, and the user's actual callback is never executed. The provider is credited the full fee regardless.

### Finding Description

`Echo.executeCallback` executes in this order:

1. `req = findActiveRequest(sequenceNumber)` — returns a `Request storage` reference, which may point to `_state.requestsOverflow[key]` when the request was evicted from the main array.
2. Provider fee is credited: `_state.providers[providerToCredit].accruedFeesInWei += (req.fee + msg.value) - pythFee` (line 161–162).
3. `clearRequest(sequenceNumber)` is called (line 164). Inside `clearRequest`, if `_state.requests[shortKey].sequenceNumber != sequenceNumber`, the branch `delete _state.requestsOverflow[key]` executes, zeroing all struct fields at that storage location.
4. The callback is dispatched: `IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(sequenceNumber, priceFeeds)` (line 177–179).

After step 3, `req.requester == address(0)` and `req.callbackGasLimit == 0`. A call to `address(0)` with 0 gas returns success in the EVM (no code at address 0). The `try` block succeeds, `emitPriceUpdate` fires, and the event log falsely signals a successful callback. The user's contract is never called.

A request lands in the overflow mapping via `allocRequest`: when a new request's `shortKey` (`uint8(keccak256(abi.encodePacked(seq))[0] & NUM_REQUESTS_MASK)`) collides with an already-active slot, the existing request is evicted to `_state.requestsOverflow`. With only `NUM_REQUESTS = 32` slots, collisions are routine under any meaningful load. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
- The user pays the full fee at request time but their callback contract is never invoked.
- The provider is credited the full fee for delivering nothing.
- No refund path exists; the funds are permanently locked in `providerInfo.accruedFeesInWei`.
- A `PriceUpdateExecuted` event is emitted, falsely indicating success and making off-chain monitoring unreliable.

### Likelihood Explanation
- With 32 slots and a keccak256-derived 5-bit shortKey, any system sustaining more than ~32 concurrent active requests will naturally produce overflow entries.
- An attacker can deterministically compute which sequence numbers share a shortKey (the hash is public and deterministic) and submit cheap requests to force a target request into overflow before calling `executeCallback`.
- Even without a deliberate attacker, the bug fires for any provider that calls `executeCallback` on an overflow request during normal high-throughput operation.

### Recommendation
Cache all fields needed after `clearRequest` into local memory variables before clearing:

```solidity
address requester = req.requester;
uint32 callbackGasLimit = req.callbackGasLimit;
// ... credit fee ...
clearRequest(sequenceNumber);
// ... use requester and callbackGasLimit for the callback ...
```

Alternatively, move `clearRequest` to after the callback invocation (matching the Entropy pattern), ensuring the storage reference remains valid throughout.

### Proof of Concept

1. Submit requests until two sequence numbers `A` and `B` share the same `shortKey = uint8(keccak256(abi.encodePacked(seq))[0] & 0x1f)`, with `A` submitted first.
2. When `B` is submitted, `allocRequest(B)` evicts request `A` to `_state.requestsOverflow[keccak256(abi.encodePacked(A))]`.
3. Call `executeCallback(provider, A, updateData, priceIds)`.
4. `findActiveRequest(A)` → `findRequest(A)` → `_state.requests[shortKey].sequenceNumber == B != A` → returns storage ref to `_state.requestsOverflow[keccak256(abi.encodePacked(A))]`.
5. Fee is credited to `provider` using `req.fee` (still valid at this point).
6. `clearRequest(A)` → `delete _state.requestsOverflow[keccak256(abi.encodePacked(A))]` → all fields zeroed.
7. `req.requester == address(0)`, `req.callbackGasLimit == 0`.
8. `IEchoConsumer(address(0))._echoCallback{gas: 0}(A, priceFeeds)` — succeeds silently.
9. `emitPriceUpdate` fires. User A's callback contract is never called. Provider keeps the fee. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-202)
```text
    function executeCallback(
        address providerToCredit,
        uint64 sequenceNumber,
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    ) external payable override {
        Request storage req = findActiveRequest(sequenceNumber);

        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

        // TODO: should this use parsePriceFeedUpdatesUnique? also, do we need to add 1 to maxPublishTime?
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);
        PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
            value: pythFee
        }(
            updateData,
            priceIds,
            SafeCast.toUint64(req.publishTime),
            SafeCast.toUint64(req.publishTime)
        );

        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
        // This should take funds from the expected provider and give to providerToCredit. The penalty should probably scale
        // with time in order to ensure that the callback eventually gets executed.
        // (There may be exploits with ^ though if the consumer contract is malicious ?)
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);

        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }

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
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L301-348)
```text
    function findActiveRequest(
        uint64 sequenceNumber
    ) internal view returns (Request storage req) {
        req = findRequest(sequenceNumber);

        if (!isActive(req) || req.sequenceNumber != sequenceNumber)
            revert NoSuchRequest();
    }

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

    function clearRequest(uint64 sequenceNumber) internal {
        (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);

        Request storage req = _state.requests[shortKey];
        if (req.sequenceNumber == sequenceNumber) {
            req.sequenceNumber = 0;
        } else {
            delete _state.requestsOverflow[key];
        }
    }

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

    function isActive(Request memory req) internal pure returns (bool) {
        return req.sequenceNumber != 0;
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L1-10)
```text
// SPDX-License-Identifier: Apache 2

pragma solidity ^0.8.0;

contract EchoState {
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
    // Maximum number of price feeds per request. This limit keeps gas costs predictable and reasonable. 10 is a reasonable number for most use cases.
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;
```
