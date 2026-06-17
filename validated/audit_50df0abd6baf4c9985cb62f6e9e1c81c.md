### Title
Callback Executed on Zeroed Storage After `clearRequest` Deletes Overflow Entry — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.sol`, `executeCallback` holds a `storage` pointer `req` to the active request, then calls `clearRequest` before invoking the user's callback. When the request lives in the overflow mapping (`_state.requestsOverflow`), `clearRequest` issues a `delete` that zeroes every field of that mapping entry. Because `req` is a Solidity `storage` pointer to the same slot, `req.requester` and `req.callbackGasLimit` are both zero at the point the callback is dispatched. The call is silently made to `address(0)` with 0 gas, the `try` block succeeds (no code at `address(0)`), `PriceUpdateExecuted` is emitted as if everything worked, and the user's actual callback contract is never invoked. The provider has already been credited and the request has been cleared, so there is no recovery path.

---

### Finding Description

`executeCallback` in `Echo.sol` follows this sequence:

1. **Line 111** – `Request storage req = findActiveRequest(sequenceNumber)` — obtains a `storage` pointer. If the request was displaced to the overflow mapping by a later request that hashed to the same `shortKey`, `req` points to `_state.requestsOverflow[key]`.

2. **Lines 161–162** – Provider fees are credited unconditionally.

3. **Line 164** – `clearRequest(sequenceNumber)` is called. Inside `clearRequest`, the overflow branch executes `delete _state.requestsOverflow[key]`, which zeroes **all** fields of that mapping entry — including `requester` and `callbackGasLimit`.

4. **Lines 176–179** – The callback is dispatched using the now-zeroed `req`:
   ```solidity
   try
       IEchoConsumer(req.requester)._echoCallback{
           gas: req.callbackGasLimit
       }(sequenceNumber, priceFeeds)
   ```
   `req.requester` is `address(0)` and `req.callbackGasLimit` is `0`. A call to `address(0)` with 0 gas succeeds (no code at that address), so the `try` block completes without reverting, `emitPriceUpdate` fires, and `PriceUpdateExecuted` is emitted — all without ever touching the user's contract.

The Entropy contract contains an explicit comment warning about exactly this pattern:

> `// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED`

and saves `callAddress` before clearing. Echo.sol omits this precaution entirely.

**How a request reaches the overflow mapping:** `allocRequest` uses an 8-bit `shortKey` derived from `keccak256(sequenceNumber)`. When a new request hashes to the same `shortKey` as an existing active request, the existing request is moved to `_state.requestsOverflow`. With only 256 primary slots and many concurrent requests, collisions are routine. An attacker can also deliberately create requests to force a victim's request into overflow.

---

### Impact Explanation

- The user's `_echoCallback` is **never executed**, even though the system records the request as successfully fulfilled.
- The user's fee is **permanently lost** — credited to the provider before the bug manifests, with no refund mechanism.
- The `PriceUpdateExecuted` event is emitted with the correct price data but the callback was silently skipped, so any on-chain state the user expected to be updated is not.
- There is **no recovery path**: the request is cleared, the fee is gone, and no retry mechanism exists.

---

### Likelihood Explanation

The overflow mapping is reached whenever two concurrent requests share the same `shortKey`. With 256 primary slots and a birthday-paradox collision probability, a system with ~20 concurrent requests has a ~50% chance of at least one collision. Any unprivileged user can submit requests to deliberately trigger collisions against a victim's in-flight request. No special privileges are required.

---

### Recommendation

Save all fields needed after `clearRequest` into local (memory) variables before calling `clearRequest`, mirroring the pattern already used in `Entropy.sol`:

```solidity
// Save before clearing
address callbackTarget = req.requester;
uint32 callbackGas    = req.callbackGasLimit;

clearRequest(sequenceNumber);
// WARNING: DO NOT USE req BELOW HERE AS ITS CONTENTS HAS BEEN CLEARED

try
    IEchoConsumer(callbackTarget)._echoCallback{gas: callbackGas}(
        sequenceNumber, priceFeeds
    )
{ ... } catch { ... }
```

---

### Proof of Concept

1. Deploy `Echo` with `NUM_REQUESTS = 256`.
2. User A calls `requestPriceUpdatesWithCallback` → assigned `sequenceNumber = N`; stored at `shortKey = K`.
3. User B calls `requestPriceUpdatesWithCallback` → assigned `sequenceNumber = M` where `requestKey(M).shortKey == K`. `allocRequest` moves User A's request to `_state.requestsOverflow[requestKey(N).key]`.
4. Provider calls `executeCallback(providerToCredit, N, updateData, priceIds)`.
   - `findActiveRequest(N)` returns `req = _state.requestsOverflow[key_N]` (User A's data is intact here).
   - Provider fees are credited.
   - `clearRequest(N)` executes `delete _state.requestsOverflow[key_N]` → `req.requester = address(0)`, `req.callbackGasLimit = 0`.
   - `IEchoConsumer(address(0))._echoCallback{gas: 0}(N, priceFeeds)` — call to `address(0)` succeeds silently.
   - `PriceUpdateExecuted` is emitted.
5. User A's callback contract was never called. User A's fee is gone. No recovery is possible.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L111-111)
```text
        Request storage req = findActiveRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-164)
```text
        _state.providers[providerToCredit].accruedFeesInWei += SafeCast
            .toUint128((req.fee + msg.value) - pythFee);

        clearRequest(sequenceNumber);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-179)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
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
