### Title
Unbounded While Loop in `Echo.executeCallback` Causes Out-of-Gas, Permanently Locking Requester Funds - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `executeCallback` function in `Echo.sol` contains an unbounded `while` loop that advances `firstUnfulfilledSeq` past all cleared requests. If many requests accumulate after the lowest-sequence-number active request, this loop iterates over every cleared slot in a single transaction, consuming unbounded gas. When the gas limit is exceeded, the entire transaction reverts — including the `clearRequest` call — leaving the victim's request permanently unfulfillable and their funds locked.

---

### Finding Description

In `Echo.sol`, `executeCallback` (lines 105–202) contains the following loop at lines 169–174:

```solidity
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
```

This loop advances the global `firstUnfulfilledSeq` pointer past every cleared (fulfilled) request until it reaches the next active one. Each iteration calls `findRequest`, which performs a storage lookup into `_state.requests[shortKey]` (a multi-field struct). The loop is unbounded: it runs once per cleared request between `firstUnfulfilledSeq` and the next active request.

**Attack path:**

1. Victim submits request with sequence number `V`.
2. Attacker submits `N` requests with sequence numbers `V+1` through `V+N`, paying the required fees.
3. Provider fulfills `V+1` through `V+N`. Each `executeCallback` call runs the while loop, but it stops immediately at `V` (still active), so `firstUnfulfilledSeq` stays at `V`.
4. Provider attempts to fulfill `V`:
   - `clearRequest(V)` executes (request `V` is now cleared in storage).
   - The while loop starts at `firstUnfulfilledSeq = V`.
   - Requests `V`, `V+1`, …, `V+N` are all cleared → loop runs **N+1 iterations**, each doing multiple storage reads and a storage write.
   - If `N` is large enough (~8,000 on a 30M-gas chain), the transaction runs out of gas and **reverts entirely**.
5. Because the transaction reverted, `clearRequest(V)` is undone — request `V` is active again.
6. Every subsequent attempt to fulfill `V` faces the same loop and always OOGs.
7. Victim's funds are **permanently locked** in the contract.

The developers themselves acknowledge the gas problem in a TODO comment immediately above the loop:

```solidity
// TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
// a better solution would be a doubly-linked list of active requests.
``` [1](#0-0) 

The `findRequest` function performs at minimum one storage read per iteration (and potentially two if the request is in the overflow mapping): [2](#0-1) 

The `clearRequest` call that precedes the loop is part of the same transaction and is fully reverted on OOG: [3](#0-2) 

---

### Impact Explanation

A requester's ETH (paid as `req.fee` at request time) is permanently locked inside the Echo contract. There is no recovery path: no admin function, no alternative claim route, and no way to advance `firstUnfulfilledSeq` without going through `executeCallback`. The stuck request blocks `firstUnfulfilledSeq` from advancing, which also degrades gas costs for all future `executeCallback` calls on other requests until the stuck one is resolved — which it never can be.

---

### Likelihood Explanation

**Medium.** The attacker must pay fees for ~8,000 requests on a 30M-gas chain (fewer on chains with lower gas limits). Each `while` iteration costs approximately 3,800–4,500 gas (warm storage reads + write to `firstUnfulfilledSeq`). The scenario also arises **without a deliberate attacker**: if a provider's off-chain system skips one request (e.g., because its `publishTime` is in the future or a transient error occurs) and continues fulfilling subsequent ones, the skipped request accumulates a growing gap. Once the gap is large enough, the skipped request can never be fulfilled. This is a realistic operational failure mode.

---

### Recommendation

1. **Replace the while loop with a doubly-linked list** of active requests (as the TODO comment already suggests). Removal from the list is O(1) and does not require scanning cleared slots.
2. **Alternatively**, remove the `firstUnfulfilledSeq` advancement from `executeCallback` entirely and expose a separate, paginated `advanceFirstUnfulfilledSeq(uint256 maxSteps)` function that keepers can call incrementally.
3. **At minimum**, cap the while loop to a fixed maximum number of iterations per call (e.g., 100) to prevent OOG, accepting that `firstUnfulfilledSeq` may lag temporarily.

---

### Proof of Concept

```solidity
// 1. Deploy Echo contract (admin, pythFee, pyth, defaultProvider, false, 0)
// 2. Victim creates request V (sequence number 1)
echoContract.requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit);
// sequenceNumber = 1

// 3. Attacker creates N = 9000 requests (sequence numbers 2..9001)
for (uint i = 0; i < 9000; i++) {
    echoContract.requestPriceUpdatesWithCallback{value: fee}(provider, publishTime, priceIds, gasLimit);
}

// 4. Provider fulfills requests 2..9001 (each call's while loop stops at seq=1)
for (uint64 seq = 2; seq <= 9001; seq++) {
    echoContract.executeCallback(provider, seq, updateData, priceIds);
    // firstUnfulfilledSeq stays at 1 after each call
}

// 5. Provider attempts to fulfill request 1
// clearRequest(1) executes, then while loop must advance 1→2→3→...→9002
// ~9001 iterations × ~3800 gas = ~34M gas → OUT OF GAS → REVERT
echoContract.executeCallback{gas: 30_000_000}(provider, 1, updateData, priceIds);
// Transaction reverts. clearRequest(1) is undone. Request 1 is still active.

// 6. Victim's funds are permanently locked. No recovery path exists.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-174)
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
