### Title
Use-After-Delete of Overflow Request Storage Causes Silent Callback Failure and Fee Loss — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, `executeCallback` calls `clearRequest(sequenceNumber)` **before** reading `req.requester` and `req.callbackGasLimit` from the same storage pointer. When a request resides in the `requestsOverflow` mapping (due to a hash collision in the 32-slot main array), `clearRequest` executes `delete _state.requestsOverflow[key]`, zeroing every field of the struct. The subsequent callback is then dispatched to `address(0)` with `gas: 0`, silently failing. The provider is still credited the fee; the user's callback is permanently lost.

The sibling contract `Entropy.sol` explicitly avoids this exact pattern by copying `req.requester` to a local variable before calling `clearRequest`, with the comment *"WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED"*. `Echo.sol` lacks this protection.

---

### Finding Description

**Storage layout — 32-slot hash table with overflow**

`EchoState.sol` defines a fixed array of 32 request slots and an overflow mapping:

```
Request[NUM_REQUESTS] requests;          // NUM_REQUESTS = 32
mapping(bytes32 => Request) requestsOverflow;
``` [1](#0-0) [2](#0-1) 

**`requestKey` produces only 32 distinct short keys**

```solidity
function requestKey(uint64 sequenceNumber)
    internal pure returns (bytes32 hash, uint8 shortHash)
{
    hash = keccak256(abi.encodePacked(sequenceNumber));
    shortHash = uint8(hash[0] & NUM_REQUESTS_MASK);   // 0x1f → 5 bits → 32 slots
}
``` [3](#0-2) 

With a monotonically increasing `sequenceNumber`, the birthday bound guarantees a collision after roughly 7–8 concurrent unfulfilled requests on average; after 33 concurrent requests a collision is certain by the pigeonhole principle.

**`allocRequest` moves the colliding incumbent to overflow**

```solidity
function allocRequest(uint64 sequenceNumber) internal returns (Request storage req) {
    (, uint8 shortKey) = requestKey(sequenceNumber);
    req = _state.requests[shortKey];
    if (isActive(req)) {
        (bytes32 reqKey, ) = requestKey(req.sequenceNumber);
        _state.requestsOverflow[reqKey] = req;   // incumbent pushed to overflow
    }
}
``` [4](#0-3) 

The incumbent (older) request is now stored in `requestsOverflow`. `findRequest` will correctly locate it there via the full 32-byte key.

**`executeCallback` reads `req` fields after `clearRequest` destroys them**

```solidity
// line 161-162: fee accounting — req.fee still valid here ✓
_state.providers[providerToCredit].accruedFeesInWei +=
    SafeCast.toUint128((req.fee + msg.value) - pythFee);

clearRequest(sequenceNumber);   // line 164 — DESTROYS overflow entry if req is in overflow

// ... loop updating firstUnfulfilledSeq ...

try
    IEchoConsumer(req.requester)._echoCallback{   // line 177 — req.requester == address(0)
        gas: req.callbackGasLimit                  // line 178 — req.callbackGasLimit == 0
    }(sequenceNumber, priceFeeds)
``` [5](#0-4) 

**`clearRequest` for an overflow entry deletes the entire struct**

```solidity
function clearRequest(uint64 sequenceNumber) internal {
    (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);
    Request storage req = _state.requests[shortKey];
    if (req.sequenceNumber == sequenceNumber) {
        req.sequenceNumber = 0;          // main array: only zero the sequence number
    } else {
        delete _state.requestsOverflow[key];  // overflow: zeroes ALL fields
    }
}
``` [6](#0-5) 

When the request is in overflow, `delete _state.requestsOverflow[key]` sets every field to zero — including `requester` → `address(0)` and `callbackGasLimit` → `0`. Because `req` is a **storage pointer** to that same slot, the subsequent read of `req.requester` and `req.callbackGasLimit` returns zeroed values.

**Contrast: `Entropy.sol` explicitly guards against this**

```solidity
address callAddress = req.requester;          // copy to local BEFORE clearRequest
EntropyStructs.Request memory reqV1 = ...;
clearRequest(provider, sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED
...
IEntropyConsumer(callAddress)._entropyCallback(...);
``` [7](#0-6) 

`Echo.sol` was written without this safeguard.

---

### Impact Explanation

For every request that ends up in `requestsOverflow`:

1. `clearRequest` zeroes `req.requester` and `req.callbackGasLimit`.
2. The `try` block dispatches `IEchoConsumer(address(0))._echoCallback{gas: 0}(...)`.
3. The call fails (no code at `address(0)`, zero gas). The `catch` block emits `PriceUpdateCallbackFailed`.
4. The provider is already credited the full fee (line 161–162 ran before `clearRequest`).
5. The user's callback is **permanently lost** — there is no retry or refund mechanism.

**Result**: Users lose the fee they paid for a callback that is silently dropped. The provider is paid for work that was not delivered to the user.

---

### Likelihood Explanation

With 32 slots and a uniform hash distribution, the expected number of requests before the first collision is ~7 (birthday problem: `√(2 × 32 × ln 2) ≈ 6.6`). On any active deployment with more than ~7 concurrent unfulfilled requests, some requests will reside in overflow. This is a normal operating condition, not an edge case.

An unprivileged attacker can also deliberately trigger this:

1. Observe a victim's request with sequence number `N`.
2. Compute `shortKey(N)`.
3. Submit enough requests to reach a sequence number `M` where `shortKey(M) == shortKey(N)` and `M > N`.
4. This pushes the victim's request to overflow.
5. When the provider fulfills sequence `N`, the callback silently fails.

No privileged access, leaked keys, or external oracle manipulation is required.

---

### Recommendation

Copy all fields needed after `clearRequest` into local (memory) variables **before** calling `clearRequest`, mirroring the pattern already used in `Entropy.sol`:

```solidity
// Save fields before clearing storage
address requester       = req.requester;
uint32  callbackGasLimit = req.callbackGasLimit;

clearRequest(sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

try
    IEchoConsumer(requester)._echoCallback{gas: callbackGasLimit}(sequenceNumber, priceFeeds)
```

---

### Proof of Concept

```solidity
// Assume shortKey(seq 1) == shortKey(seq 33) (deterministic, computable off-chain)

// Step 1: User A submits request → seq 1, stored in _state.requests[shortKey]
echo.requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit);

// Step 2: Attacker submits 32 more requests until seq 33 collides with seq 1
// seq 33 → shortKey collision → seq 1 moved to requestsOverflow[keccak256(abi.encodePacked(1))]

// Step 3: Provider fulfills seq 1
echo.executeCallback(provider, 1, updateData, priceIds);
//   → findActiveRequest(1) returns storage pointer into requestsOverflow
//   → fee credited to provider ✓
//   → clearRequest(1) → delete requestsOverflow[key(1)] → req.requester = address(0), req.callbackGasLimit = 0
//   → IEchoConsumer(address(0))._echoCallback{gas: 0}(...) → silent failure
//   → PriceUpdateCallbackFailed emitted
//   → User A's callback never executed; fee permanently lost
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-7)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L66-67)
```text
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-179)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L281-286)
```text
    function requestKey(
        uint64 sequenceNumber
    ) internal pure returns (bytes32 hash, uint8 shortHash) {
        hash = keccak256(abi.encodePacked(sequenceNumber));
        shortHash = uint8(hash[0] & NUM_REQUESTS_MASK);
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
