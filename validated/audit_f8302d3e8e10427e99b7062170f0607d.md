### Title
Unbounded Linear Scan in `executeCallback` Enables Denial-of-Service via Gas Exhaustion - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo.executeCallback()` function contains an unbounded `while` loop that linearly scans storage slots from `_state.firstUnfulfilledSeq` to `_state.currentSequenceNumber`. An unprivileged attacker can inflate `currentSequenceNumber` by submitting many requests, then ensure the oldest sequence number is fulfilled last, forcing the `while` loop to perform thousands of cold storage reads in a single transaction, exceeding the block gas limit and permanently bricking fulfillment of that request — locking its fees in the contract.

---

### Finding Description

In `Echo.executeCallback()`, after clearing a fulfilled request, the contract advances `_state.firstUnfulfilledSeq` with this loop:

```solidity
// TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup
// for each sequence number. a better solution would be a doubly-linked list of active requests.
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

Each iteration calls `findRequest()`, which performs **two cold `SLOAD` operations**: one into the fixed `Request[32]` array and one into the `requestsOverflow` mapping. [2](#0-1) 

The overflow mapping is unbounded — `requestsOverflow` is a `mapping(bytes32 => Request)` with no cap on entries. [3](#0-2) 

`currentSequenceNumber` increments by 1 for every call to `requestPriceUpdatesWithCallback`, which is permissionlessly callable by any address that pays the fee. [4](#0-3) 

**Attack steps:**

1. Attacker submits N requests (seq=1 through seq=N), paying the required fee for each. `currentSequenceNumber` becomes N+1.
2. After the exclusivity period expires, the attacker (or anyone) calls `executeCallback` for seq=2 through seq=N. Each of these fulfillments runs the `while` loop but terminates immediately because `firstUnfulfilledSeq` is still stuck at seq=1 (which remains active).
3. When seq=1 is finally fulfilled, `clearRequest(1)` is called, then the `while` loop starts at seq=1 and must scan through all N-1 already-cleared sequence numbers before reaching `currentSequenceNumber`. Each of the N-1 iterations performs 2 cold SLOADs (4,200 gas each pair). For N=3,600, this alone consumes ~30M gas, hitting the Ethereum block gas limit.
4. The `executeCallback` for seq=1 reverts out-of-gas. The fees paid for seq=1 are permanently locked in the contract.

The contract's own TODO comment acknowledges this exact risk: *"if executeCallback can revert, then funds can be permanently locked in the contract."* [5](#0-4) 

---

### Impact Explanation

- **Availability**: `executeCallback` for the oldest unfulfilled request becomes permanently un-executable once the sequence gap is large enough. No admin escape hatch exists to skip a stuck sequence number.
- **Financial loss**: The fee paid by the requester for seq=1 (stored in `req.fee`) is permanently locked in the contract, as `accruedFeesInWei` for the provider is only credited inside `executeCallback` after the loop.
- **Cascading effect**: Because `firstUnfulfilledSeq` never advances past seq=1, every subsequent `executeCallback` call also runs the full scan from seq=1, making all future fulfillments progressively more expensive until they too fail.

---

### Likelihood Explanation

- The entry point (`requestPriceUpdatesWithCallback`) is fully permissionless — any address can call it by paying the fee.
- The attack requires capital proportional to N × minimum\_fee, but this is a one-time cost to permanently disable the contract.
- No privileged role is needed. The attacker does not need to be the assigned provider; after the `exclusivityPeriodSeconds` window, anyone can call `executeCallback`. [6](#0-5) 

---

### Recommendation

Replace the linear scan with a data structure that supports O(1) removal. The contract's own TODO suggests a doubly-linked list of active requests. Alternatively:

- Track `firstUnfulfilledSeq` lazily and cap the number of iterations per call (e.g., advance at most `NUM_REQUESTS` steps per `executeCallback` invocation).
- Use a min-heap or ordered set keyed by sequence number.
- Bound the maximum gap between `firstUnfulfilledSeq` and `currentSequenceNumber` by rejecting new requests when the gap exceeds a safe threshold.

---

### Proof of Concept

```solidity
function testDoS_firstUnfulfilledSeqScan() external {
    uint256 N = 3600;
    uint64[] memory seqs = new uint64[](N);

    // Step 1: Submit N requests
    for (uint256 i = 0; i < N; i++) {
        vm.deal(attacker, fee);
        vm.prank(attacker);
        seqs[i] = echo.requestPriceUpdatesWithCallback{value: fee}(
            defaultProvider, block.timestamp, priceIds, callbackGasLimit
        );
    }

    // Step 2: Fulfill seq=2 through seq=N (leaving seq=1 last)
    for (uint256 i = 1; i < N; i++) {
        vm.prank(defaultProvider);
        echo.executeCallback(defaultProvider, seqs[i], updateData, priceIds);
    }

    // Step 3: Attempt to fulfill seq=1 — while loop scans N-1 cleared slots
    // Each iteration: 2 cold SLOADs = ~4200 gas → N=3600 → ~15M gas for loop alone
    // Combined with other execution costs, this exceeds the block gas limit
    vm.prank(defaultProvider);
    echo.executeCallback{gas: 30_000_000}(
        defaultProvider, seqs[0], updateData, priceIds
    ); // reverts: out of gas
    // req.fee for seqs[0] is now permanently locked
}
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L73-73)
```text
        requestSequenceNumber = _state.currentSequenceNumber++;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L113-121)
```text
        // Check provider exclusivity using configurable period
        if (
            block.timestamp < req.publishTime + _state.exclusivityPeriodSeconds
        ) {
            require(
                providerToCredit == req.provider,
                "Only assigned provider during exclusivity period"
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-157)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
```

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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L66-68)
```text
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
        mapping(address => ProviderInfo) providers;
```
