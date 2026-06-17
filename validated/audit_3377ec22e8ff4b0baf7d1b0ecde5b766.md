### Title
Unbounded `firstUnfulfilledSeq` Scan in `Echo.executeCallback` Enables Permanent DoS and Fund Locking — (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.executeCallback` contains an unbounded `while` loop that linearly scans all fulfilled sequence numbers before invoking the consumer callback. An attacker can create a large number of cheap requests and fulfill them out of order, causing the scan to exceed the block gas limit when the earliest unfulfilled request is eventually processed. Because the transaction reverts entirely when the loop runs out of gas, the victim's request can never be fulfilled and their funds are permanently locked.

---

### Finding Description

Inside `executeCallback`, after clearing the current request from storage, the contract advances `_state.firstUnfulfilledSeq` by scanning forward through every sequence number until it finds the next active one:

```solidity
clearRequest(sequenceNumber);

while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

Each iteration calls `findRequest`, which performs two cold `SLOAD` operations (~4,200 gas each) — one against the fixed-size `requests[32]` array and one against the `requestsOverflow` mapping:

```solidity
function findRequest(uint64 sequenceNumber) internal view returns (Request storage req) {
    (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);
    req = _state.requests[shortKey];
    if (req.sequenceNumber == sequenceNumber) {
        return req;
    } else {
        req = _state.requestsOverflow[key];
    }
}
``` [2](#0-1) 

There is no upper bound on the number of iterations. The loop runs from `firstUnfulfilledSeq` all the way to `currentSequenceNumber`, which is a global counter that increments with every call to `requestPriceUpdatesWithCallback`: [3](#0-2) 

The fee calculation in `getFee` does not account for the gas cost of this scan — it only prices the `callbackGasLimit`:

```solidity
uint256 gasFee = callbackGasLimit * providerFeeInWei;
feeAmount = baseFee + providerBaseFee + providerFeedFee + SafeCast.toUint96(gasFee);
``` [4](#0-3) 

The developers themselves acknowledge the loop is expensive and note that `executeCallback` must never revert to avoid permanently locking funds: [5](#0-4) [6](#0-5) 

---

### Impact Explanation

If the while loop causes an out-of-gas revert, the entire `executeCallback` transaction reverts — including `clearRequest` and the provider fee credit. The request remains active but can never be fulfilled if the required gas exceeds the block gas limit. The user's deposited fee (`req.fee`) is permanently locked in the contract with no retry or refund mechanism.

On Ethereum mainnet (30M gas block limit), approximately 7,143 cold-storage iterations exhaust the block. On L2s the block gas limit is higher, but the SLOAD cost is identical, so the threshold scales proportionally.

---

### Likelihood Explanation

The attack is reachable by any unprivileged user. The attacker:

1. Calls `requestPriceUpdatesWithCallback` with `callbackGasLimit = 0` (minimising fees) for N requests, where N ≥ ~7,143 on mainnet.
2. Fulfills requests 2 through N immediately (each fulfillment terminates the while loop after 1–2 iterations because request 1 is still active).
3. Leaves request 1 (the victim's or their own earliest request) unfulfilled.
4. When anyone attempts to fulfill request 1, the while loop must scan all N−1 fulfilled slots, exceeding the block gas limit and reverting.

On L2 networks where gas is cheap (Arbitrum, Base, Optimism), the cost of creating thousands of requests is negligible, making this attack economically viable.

---

### Recommendation

- Replace the unbounded linear scan with a bounded or lazy update. For example, cap the number of iterations per call (e.g., advance at most 256 slots per `executeCallback`), or use a doubly-linked list of active requests (as the in-code TODO already suggests).
- Add a `gasleft()` guard before the while loop to revert early with a descriptive error rather than silently running out of gas mid-scan.
- Ensure the fee charged to the requester accounts for the worst-case scan cost, not just the callback gas limit.

---

### Proof of Concept

```solidity
// 1. Attacker creates N requests with callbackGasLimit = 0 (minimal fee)
for (uint i = 0; i < N; i++) {
    echo.requestPriceUpdatesWithCallback{value: minFee}(
        provider, block.timestamp, priceIds, 0
    );
}
// sequenceNumbers: 1 .. N, firstUnfulfilledSeq = 0

// 2. Attacker fulfills requests 2..N (while loop terminates after 1-2 iters each time)
for (uint i = 2; i <= N; i++) {
    echo.executeCallback(provider, i, updateData, priceIds);
}
// firstUnfulfilledSeq still = 0 (request 1 is still active)

// 3. Victim (or attacker) tries to fulfill request 1
// executeCallback calls clearRequest(1), then enters the while loop
// Loop iterates from 0 to N+1 (~N cold SLOADs × 4200 gas = N × 4200 gas)
// For N = 7143: 7143 × 4200 ≈ 30M gas → OOG revert on Ethereum mainnet
// Transaction reverts; request 1 is never cleared; victim's funds are locked forever.
echo.executeCallback(provider, 1, updateData, priceIds); // REVERTS with OOG
``` [1](#0-0) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L73-73)
```text
        requestSequenceNumber = _state.currentSequenceNumber++;
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-157)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L164-174)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L248-254)
```text
        uint96 providerFeeInWei = _state.providers[provider].feePerGasInWei; // Provider's per-gas rate
        uint256 gasFee = callbackGasLimit * providerFeeInWei; // Total provider fee based on gas
        feeAmount =
            baseFee +
            providerBaseFee +
            providerFeedFee +
            SafeCast.toUint96(gasFee); // Total fee user needs to pay
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-10)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
    // Maximum number of price feeds per request. This limit keeps gas costs predictable and reasonable. 10 is a reasonable number for most use cases.
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;
```
