### Title
Unbounded `while` Loop in `executeCallback` Enables Gas-Exhaustion DoS That Permanently Locks Request Fees — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.executeCallback` contains an unbounded `while` loop that advances `_state.firstUnfulfilledSeq` by calling `findRequest()` — a storage-reading function — for every sequence number between `firstUnfulfilledSeq` and `currentSequenceNumber`. An unprivileged attacker can inflate `currentSequenceNumber` by creating many requests, then fulfill them out of order so that the final `executeCallback` call must scan the entire range in one transaction, exhausting the block gas limit and permanently bricking fulfillment of the targeted request.

---

### Finding Description

In `Echo.sol`, after clearing a fulfilled request, the following loop runs unconditionally:

```solidity
// Echo.sol lines 169-174
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

Each iteration calls `findRequest`, which performs at least one SLOAD against the fixed-size `requests[32]` array and, for overflow entries, a second SLOAD against the `requestsOverflow` mapping:

```solidity
// Echo.sol lines 310-321
function findRequest(uint64 sequenceNumber) internal view returns (Request storage req) {
    (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);
    req = _state.requests[shortKey];
    if (req.sequenceNumber == sequenceNumber) {
        return req;
    } else {
        req = _state.requestsOverflow[key];   // cold SLOAD per unique key
    }
}
``` [2](#0-1) 

Cleared overflow entries (`delete _state.requestsOverflow[key]`) each require a **cold SLOAD (2,100 gas)** on the next read because the slot is zeroed but the key is unique per sequence number. The developers themselves flagged this in a TODO comment immediately above the loop:

> "I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number." [3](#0-2) 

`requestPriceUpdatesWithCallback` is fully permissionless — any address can call it and increment `currentSequenceNumber`: [4](#0-3) 

After the configurable `exclusivityPeriodSeconds`, `executeCallback` is also callable by any address, not just the assigned provider: [5](#0-4) 

---

### Impact Explanation

When the while loop's iteration count exceeds roughly **14,285** (≈ 30,000,000 block gas / 2,100 gas per cold SLOAD), the `executeCallback` transaction for the targeted request reverts every time it is attempted. The request's `fee` field — paid by the original requester — is permanently locked in the contract with no withdrawal path for the requester. The provider also cannot collect their fee. The requester's downstream callback is never executed, breaking any protocol logic that depends on it.

---

### Likelihood Explanation

`requestPriceUpdatesWithCallback` requires paying `getFee(provider, callbackGasLimit, priceIds)`. With a provider that sets minimal fees (`baseFeeInWei`, `feePerFeedInWei`, `feePerGasInWei` all near zero) and a low `callbackGasLimit`, the per-request cost approaches only the Pyth protocol fee (`pythFeeInWei`). An attacker willing to spend a modest amount of ETH can create the required number of requests. The attack is amplified if the attacker registers their own provider with zero fees. The attack is realistic on any EVM chain where Echo is deployed with low gas costs (e.g., L2s).

---

### Recommendation

Replace the unbounded `while` loop with a **doubly-linked list** of active requests (as the TODO comment itself suggests), or cap the number of iterations per call (e.g., advance at most `MAX_SCAN_STEPS` per `executeCallback` invocation). Additionally, enforce a meaningful minimum fee per request to raise the economic cost of inflating `currentSequenceNumber`.

---

### Proof of Concept

1. Attacker registers a provider with zero fees (or uses an existing low-fee provider).
2. Attacker calls `requestPriceUpdatesWithCallback` N times (e.g., N = 20,000), creating sequence numbers 1…N. All requests land in `requestsOverflow` after the 32-slot fixed array fills up.
3. After `exclusivityPeriodSeconds` elapses, attacker calls `executeCallback` for sequences 2…N (in any order). Each call's `while` loop stops at sequence 1 (still active), so `firstUnfulfilledSeq` remains at 1.
4. Victim (or provider) attempts to call `executeCallback` for sequence 1. The `while` loop now iterates from 1 to N, performing ~N cold SLOADs against `requestsOverflow`. At N = 20,000 and 2,100 gas/SLOAD, the loop alone consumes ~42,000,000 gas — exceeding Ethereum's ~30,000,000 block gas limit.
5. Every attempt to fulfill sequence 1 reverts. The fee locked in `req.fee` for sequence 1 is permanently inaccessible. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-76)
```text
    function requestPriceUpdatesWithCallback(
        address provider,
        uint64 publishTime,
        bytes32[] calldata priceIds,
        uint32 callbackGasLimit
    ) external payable override returns (uint64 requestSequenceNumber) {
        require(
            _state.providers[provider].isRegistered,
            "Provider not registered"
        );

        // FIXME: this comment is wrong. (we're not using tx.gasprice)
        // NOTE: The 60-second future limit on publishTime prevents a DoS vector where
        //      attackers could submit many low-fee requests for far-future updates when gas prices
        //      are low, forcing executors to fulfill them later when gas prices might be much higher.
        //      Since tx.gasprice is used to calculate fees, allowing far-future requests would make
        //      the fee estimation unreliable.
        require(publishTime <= block.timestamp + 60, "Too far in future");
        if (priceIds.length > MAX_PRICE_IDS) {
            revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
        }
        requestSequenceNumber = _state.currentSequenceNumber++;

        uint96 requiredFee = getFee(provider, callbackGasLimit, priceIds);
        if (msg.value < requiredFee) revert InsufficientFee();
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L166-168)
```text
        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
        // After successful callback, update firstUnfulfilledSeq if needed
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L169-174)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-7)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```
