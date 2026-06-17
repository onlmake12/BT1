### Title
Unbounded `while` Loop in `Echo.executeCallback` Enables Out-of-Gas DoS, Permanently Locking Requester Funds — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` contains an unbounded `while` loop that linearly scans forward through all sequence numbers from `_state.firstUnfulfilledSeq` to `_state.currentSequenceNumber` after each fulfillment. An unprivileged attacker can create a large number of requests, fulfill all but the earliest one, and then cause the final `executeCallback` call for that earliest request to revert with out-of-gas. Because the revert undoes `clearRequest`, the request remains permanently active but unfulfillable, locking the requester's funds indefinitely.

---

### Finding Description

In `Echo.sol`, after `clearRequest(sequenceNumber)` is called, the following loop executes:

```solidity
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

Each iteration calls `findRequest`, which performs two storage reads: one into the fixed-size ring buffer `_state.requests[shortKey]` and, for overflow requests, one into `_state.requestsOverflow[key]`. [2](#0-1) 

The ring buffer holds only `NUM_REQUESTS = 32` slots. [3](#0-2) 

Any request beyond the 32 active slots is stored in `requestsOverflow`, a `mapping(bytes32 => Request)`. [4](#0-3) 

Each cold `SLOAD` of a distinct mapping key costs ~2,100 gas (EIP-2929). For N overflow requests, the while loop costs approximately `N × 2,100` gas.

**Attack path:**

1. Attacker calls `requestPriceUpdatesWithCallback` N times, creating sequence numbers 1 through N. Each call pays the required fee.
2. Attacker (or any executor) calls `executeCallback` for requests 2 through N in any order. Each time, the while loop starts at `firstUnfulfilledSeq = 1`, finds request 1 still active, and exits immediately (0 iterations). `firstUnfulfilledSeq` never advances.
3. When `executeCallback` is finally called for request 1:
   - `clearRequest(1)` marks it inactive.
   - The while loop starts at seq 1 (now inactive), then scans 2, 3, …, N — all already fulfilled — performing N−1 cold storage reads into `requestsOverflow`.
   - At N ≈ 15,000: `15,000 × 2,100 ≈ 31.5M gas`, exceeding Ethereum's ~30M block gas limit.
   - The entire transaction reverts, undoing `clearRequest(1)`.
4. Request 1 is now permanently unfulfillable: every subsequent attempt to call `executeCallback` for it will also hit the same loop and revert. The requester's funds are locked forever.

The `MAX_PRICE_IDS = 10` cap limits the fee per request but does not bound the total number of requests. [5](#0-4) 

The existing gas benchmark test explicitly acknowledges this linear scan as the worst-case scenario: [6](#0-5) 

---

### Impact Explanation

- **Permanent fund lock:** The requester's ETH (fee paid at request time) is locked in the contract with no withdrawal path. `executeCallback` is the only way to clear a request; there is no timeout or refund mechanism visible in the contract.
- **Callback never executes:** The consumer contract's `_echoCallback` is never invoked, breaking any downstream protocol logic that depends on the price update.
- **Scope match:** This is a direct smart-contract fund-locking vulnerability on an EVM target chain, within Pyth's Immunefi scope for the Echo/Pulse product.

---

### Likelihood Explanation

- Any unprivileged address can call `requestPriceUpdatesWithCallback` and `executeCallback`.
- The attacker must pay fees for N requests. At a low per-request fee (e.g., `pythFeeInWei` set to a small value), the economic cost to reach the gas-limit threshold (~15,000 requests) may be modest relative to the value locked in the victim request.
- The attack is amplified if the victim is a high-value request (large `callbackGasLimit` → large fee locked).
- The test file confirms the developers are aware of the linear scan but have not bounded it.

---

### Recommendation

Replace the unbounded `while` loop with a bounded scan (e.g., advance at most `MAX_SCAN_STEPS` per call), or switch to a doubly-linked list of active requests (as noted in the inline TODO comment at line 167): [7](#0-6) 

A doubly-linked list would allow O(1) removal and O(1) advancement of `firstUnfulfilledSeq`, eliminating the linear scan entirely. Alternatively, cap the total number of outstanding requests per provider or globally to bound the worst-case loop length.

---

### Proof of Concept

```solidity
// Pseudocode PoC
uint N = 15_000;
uint64[] memory seqs = new uint64[](N);

// Step 1: Create N requests (attacker pays fee * N)
for (uint i = 0; i < N; i++) {
    seqs[i] = echo.requestPriceUpdatesWithCallback{value: fee}(
        provider, block.timestamp, priceIds, callbackGasLimit
    );
}

// Step 2: Fulfill requests 2..N (firstUnfulfilledSeq stays at seqs[0])
for (uint i = 1; i < N; i++) {
    echo.executeCallback(provider, seqs[i], updateData, priceIds);
}

// Step 3: Attempt to fulfill request 1 — while loop runs ~15,000 iterations
// → OOG revert → request 1 permanently locked
echo.executeCallback(provider, seqs[0], updateData, priceIds); // REVERTS
```

The `while` loop at `Echo.sol:169-174` iterates over all N−1 fulfilled (inactive) sequence numbers, each requiring a cold `SLOAD` into `requestsOverflow`, consuming gas proportional to N and exceeding the block gas limit for sufficiently large N.

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L166-174)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-7)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L9-10)
```text
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L66-68)
```text
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
        mapping(address => ProviderInfo) providers;
```

**File:** target_chains/ethereum/contracts/test/EchoGasBenchmark.t.sol (L121-130)
```text
    // This test checks the gas usage for worst-case out-of-order fulfillment.
    // It creates 10 requests, and then fulfills them in reverse order.
    //
    // The last fulfillment will be the most expensive since it needs
    // to linearly scan through all the fulfilled requests in storage
    // in order to update _state.lastUnfulfilledReq
    //
    // NOTE: Run test with `forge test --gas-report --match-test testMultipleRequestsOutOfOrderFulfillment`
    // and observe the `max` value for `executeCallback` to see the cost of the most expensive request.
    function testMultipleRequestsOutOfOrderFulfillment() public {
```
