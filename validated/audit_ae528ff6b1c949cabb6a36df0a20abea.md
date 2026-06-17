### Title
Unbounded `while` Loop in `executeCallback` Outside `try/catch` Can Permanently Lock Request Funds - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol::executeCallback` contains an unbounded `while` loop that advances `_state.firstUnfulfilledSeq` by scanning all sequence numbers between `firstUnfulfilledSeq` and `currentSequenceNumber`. This loop runs **outside** the `try/catch` block that wraps the consumer callback. If the loop consumes enough gas to cause an out-of-gas revert, the entire transaction reverts — including the `clearRequest` and fee-credit effects — leaving the request permanently unfulfillable and its funds locked in the contract. The contract's own developer TODO comment at line 155 explicitly acknowledges this risk.

---

### Finding Description

In `Echo.sol`, `executeCallback` performs the following sequence:

1. Credits provider fees (line 161–162)
2. Clears the request (line 164)
3. **Runs an unbounded `while` loop** to advance `_state.firstUnfulfilledSeq` (lines 169–174)
4. Calls the consumer callback inside a `try/catch` (lines 176–201) [1](#0-0) 

The `while` loop at step 3 iterates from `_state.firstUnfulfilledSeq` up to `_state.currentSequenceNumber`, calling `findRequest` (a storage lookup) on every iteration:

```solidity
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [2](#0-1) 

`_state.currentSequenceNumber` is a monotonically increasing counter with no upper bound enforced at the contract level. Each `findRequest` call performs a cold SLOAD (~2100 gas) or warm SLOAD (~100 gas) against either the fixed `requests[32]` array or the `requestsOverflow` mapping. [3](#0-2) 

The loop is **not** inside the `try/catch` block. A Solidity `try` statement only catches reverts from **external calls**; any revert occurring in the surrounding function body (including this loop) propagates normally and reverts the entire transaction. This means if the loop runs out of gas, all state changes in `executeCallback` — including `clearRequest` and the fee credit — are rolled back, and the request remains active but permanently unfulfillable.

There is no `cancelRequest`, `refundRequest`, or any other recovery path in the contract. [4](#0-3) 

The developer's own TODO at line 155 acknowledges: *"if executeCallback can revert, then funds can be permanently locked in the contract."*

---

### Impact Explanation

A request's fee (paid by the requester as `msg.value`) is stored in `req.fee` and only released to the provider inside `executeCallback`. If `executeCallback` always reverts due to the unbounded loop, the fee is permanently locked in the Echo contract with no recovery mechanism. The requester loses their funds and never receives their price update callback. [5](#0-4) 

---

### Likelihood Explanation

An unprivileged attacker can trigger this condition:

1. Submit request #1 (the "victim" request) with a normal fee.
2. Submit a large number of additional requests (#2 through #N), paying the required fees.
3. Have requests #2–#N fulfilled by the provider (or wait for the exclusivity period to expire and fulfill them as any caller). Each fulfillment of #2–#N stops the while loop immediately because `firstUnfulfilledSeq` still points to the active request #1.
4. After all requests #2–#N are cleared, `firstUnfulfilledSeq` remains at #1. When request #1 is finally fulfilled, the while loop must scan from sequence #1 through #N — an O(N) storage scan.
5. With a sufficiently large N (e.g., tens of thousands of requests on a low-fee chain), the while loop exhausts the block gas limit, causing `executeCallback` to always revert for request #1.

The attacker pays fees for requests #2–#N, but on chains with low gas costs (e.g., Arbitrum, Base, BNB Chain where Echo is deployed), this is economically viable. The attacker does not need any privileged role. [6](#0-5) 

---

### Recommendation

1. **Cap the while loop iterations** to a fixed maximum (e.g., `NUM_REQUESTS = 32`) per `executeCallback` call, accepting that `firstUnfulfilledSeq` may not advance fully in a single call.
2. Alternatively, **remove the `firstUnfulfilledSeq` advancement from `executeCallback`** entirely and make it a separate, explicitly gas-bounded maintenance function.
3. Add a `gasleft()` guard before the loop to abort gracefully rather than reverting the entire transaction.
4. Implement a request cancellation/refund path so that stuck requests can be recovered. [7](#0-6) 

---

### Proof of Concept

```
Setup:
- Echo contract deployed with defaultProvider registered
- Attacker is an unprivileged user

Step 1: Attacker calls requestPriceUpdatesWithCallback() N times (e.g., N = 50,000)
        paying the required fee each time. Sequence numbers 1..N are assigned.

Step 2: Provider fulfills requests 2..N via executeCallback().
        For each fulfillment, the while loop checks firstUnfulfilledSeq (= 1, still active)
        and exits immediately. firstUnfulfilledSeq stays at 1.

Step 3: Provider attempts to fulfill request #1 via executeCallback().
        The while loop now iterates from seq=1 to seq=N, calling findRequest()
        (storage lookup) on each of the N-1 cleared slots.
        Gas consumed: ~2100 * N gas for cold SLOADs.
        At N = 50,000: ~105,000,000 gas >> block gas limit (~30M on mainnet).
        Transaction reverts with out-of-gas.

Result: Request #1 can never be fulfilled. req.fee is permanently locked.
        No refund mechanism exists.
``` [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-72)
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L78-84)
```text
        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L155-157)
```text
        // TODO: if this effect occurs here, we need to guarantee that executeCallback can never revert.
        // If executeCallback can revert, then funds can be permanently locked in the contract.
        // TODO: there also needs to be some penalty mechanism in case the expected provider doesn't execute the callback.
```

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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-10)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
    // Maximum number of price feeds per request. This limit keeps gas costs predictable and reasonable. 10 is a reasonable number for most use cases.
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;
```
