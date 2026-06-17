### Title
Permanently Stuck `firstUnfulfilledSeq` Pointer Causes Unbounded Iteration in `getFirstActiveRequests()` — (`File: target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.sol` maintains `_state.firstUnfulfilledSeq` as an optimization pointer so that `getFirstActiveRequests()` does not need to scan from sequence number 0 on every call. An unprivileged requester can permanently freeze this pointer by submitting a request with an unfulfillable `publishTime` (e.g., `publishTime = 1`). Because `clearRequest()` is only reachable through `executeCallback()`, which requires `parsePriceFeedUpdates` to succeed with the exact stored `publishTime`, no one can ever clear that request. The pointer is stuck forever, and `getFirstActiveRequests()` must linearly scan every sequence number from the stuck position to `currentSequenceNumber` on every invocation.

### Finding Description

`Echo.sol` stores `_state.firstUnfulfilledSeq` to mark the lower bound for scanning active requests:

```solidity
// After successful callback, update firstUnfulfilledSeq if needed
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

This pointer only advances past a sequence number when `isActive(findRequest(seq))` returns false — i.e., when the request at that position has been cleared. The only code path that clears a request is `clearRequest(sequenceNumber)` inside `executeCallback()`: [2](#0-1) 

`clearRequest` is reached only **after** `parsePriceFeedUpdates` succeeds:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{value: pythFee}(
    updateData, priceIds,
    SafeCast.toUint64(req.publishTime),   // minPublishTime
    SafeCast.toUint64(req.publishTime)    // maxPublishTime
);
// ...
clearRequest(sequenceNumber);
``` [3](#0-2) 

`parsePriceFeedUpdates` requires the supplied price data to have a `publishTime` **exactly equal** to `req.publishTime`. There is no lower bound on `publishTime` in `requestPriceUpdatesWithCallback()`:

```solidity
require(publishTime <= block.timestamp + 60, "Too far in future");
``` [4](#0-3) 

An attacker sets `publishTime = 1` (Unix epoch + 1 second). No real Pyth price data carries that timestamp, so `parsePriceFeedUpdates` will always revert for this request. `clearRequest` is never reached, the request remains permanently active, and `firstUnfulfilledSeq` is stuck at that sequence number indefinitely.

`getFirstActiveRequests()` always starts its scan from `firstUnfulfilledSeq`:

```solidity
uint64 currentSeq = _state.firstUnfulfilledSeq;
while (actualCount < count && currentSeq < _state.currentSequenceNumber) {
    Request memory req = findRequest(currentSeq);
    if (isActive(req)) { ... }
    currentSeq++;
}
``` [5](#0-4) 

With the pointer frozen, every call to `getFirstActiveRequests()` must iterate over the entire history of requests from the stuck position to `currentSequenceNumber`. The IEcho interface itself documents this linear scaling:

> "The function starts from firstUnfulfilledSeq (all requests before this are fulfilled) and scans forward until it finds enough active requests or reaches currentSequenceNumber." [6](#0-5) 

The `findRequest` function correctly follows requests that have been moved to the overflow mapping, so the stuck request remains findable and active even after its storage slot is reused by later requests: [7](#0-6) 

### Impact Explanation

`getFirstActiveRequests()` is the primary mechanism by which the provider's keeper service discovers pending requests to fulfill. As `currentSequenceNumber` grows while `firstUnfulfilledSeq` stays frozen, every call to this view function performs an O(N) storage scan over the entire request history. On RPC nodes with `eth_call` gas caps (common on third-party providers), the call will eventually exceed the limit and return an error, blinding the keeper to all pending requests. Even before that threshold, the growing scan cost degrades keeper responsiveness and increases RPC quota consumption.

### Likelihood Explanation

The attack requires a single transaction calling `requestPriceUpdatesWithCallback` with `publishTime = 1` and paying the required fee. No special privileges are needed. The fee is the only cost to the attacker. The condition is permanent and irreversible — there is no admin function to cancel or skip a stuck request.

### Recommendation

1. **Add a minimum `publishTime` bound** in `requestPriceUpdatesWithCallback`, e.g., `require(publishTime >= block.timestamp - MAX_AGE)`, to prevent requests for timestamps for which no Pyth data will ever exist.
2. **Add an admin/governance function** to forcibly cancel a stuck request (analogous to the report's suggestion of allowing the owner to forcefully withdraw a queued withdrawal).
3. **Replace the linear scan** in `getFirstActiveRequests()` with a doubly-linked list of active requests (the code already contains a TODO comment noting this: `// a better solution would be a doubly-linked list of active requests`). [8](#0-7) 

### Proof of Concept

```solidity
// 1. Attacker creates an unfulfillable request (publishTime = 1, epoch + 1s)
echo.requestPriceUpdatesWithCallback{value: fee}(
    defaultProvider,
    1,          // publishTime = 1 — no Pyth data will ever match this
    priceIds,
    callbackGasLimit
);
// Sequence number N is now permanently active.

// 2. Normal users create and fulfill many requests (N+1, N+2, ..., N+1000000).
//    firstUnfulfilledSeq advances to N but stops there because request N is still active.

// 3. Provider's keeper calls getFirstActiveRequests(10).
//    The function iterates from seq N to currentSequenceNumber (N+1000000),
//    performing 1,000,000 storage reads to find 10 active requests.
//    On a node with a 50M gas eth_call cap this will revert, blinding the keeper.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L69-69)
```text
        require(publishTime <= block.timestamp + 60, "Too far in future");
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L143-164)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L166-167)
```text
        // TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
        // a better solution would be a doubly-linked list of active requests.
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L168-174)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L478-490)
```text
        uint64 currentSeq = _state.firstUnfulfilledSeq;

        // Continue until we find enough active requests or reach current sequence
        while (
            actualCount < count && currentSeq < _state.currentSequenceNumber
        ) {
            Request memory req = findRequest(currentSeq);
            if (isActive(req)) {
                requests[actualCount] = req;
                actualCount++;
            }
            currentSeq++;
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/IEcho.sol (L148-153)
```text
     * @dev Gas Usage: This function's gas cost scales linearly with the number of requests
     *      between firstUnfulfilledSeq and currentSequenceNumber. Each iteration costs approximately:
     *      - 2100 gas for cold storage reads, 100 gas for warm storage reads (SLOAD)
     *      - Additional gas for array operations
     *      The function starts from firstUnfulfilledSeq (all requests before this are fulfilled)
     *      and scans forward until it finds enough active requests or reaches currentSequenceNumber.
```
