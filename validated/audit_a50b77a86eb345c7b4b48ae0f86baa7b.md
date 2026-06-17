### Title
Unbounded `while` Loop in `executeCallback` Enables Permanent DoS on Request Fulfillment — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `executeCallback` function in `Echo.sol` contains an unbounded `while` loop that linearly scans sequence numbers to advance `firstUnfulfilledSeq`. An unprivileged attacker who registers as a provider can create a large number of requests and fulfill all but the earliest one, causing the while loop to iterate over the entire fulfilled range when the earliest request is finally processed. If the iteration count is large enough, the transaction exceeds the block gas limit, permanently preventing the earliest request's callback from ever executing and locking the requester's funds.

---

### Finding Description

Inside `executeCallback`, after `clearRequest` is called, the following loop runs unconditionally:

```solidity
// Echo.sol lines 169–174
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
```

The loop advances `firstUnfulfilledSeq` one step at a time, calling `findRequest` (which performs 1–2 `SLOAD` operations per iteration) for every sequence number between the current `firstUnfulfilledSeq` and the next active request.

The developer comment directly above this loop acknowledges the problem:

```solidity
// TODO: I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number.
// a better solution would be a doubly-linked list of active requests.
```

`findRequest` performs a hash-based lookup:

```solidity
// Echo.sol lines 310–321
function findRequest(uint64 sequenceNumber) internal view returns (Request storage req) {
    (bytes32 key, uint8 shortKey) = requestKey(sequenceNumber);
    req = _state.requests[shortKey];
    if (req.sequenceNumber == sequenceNumber) {
        return req;
    } else {
        req = _state.requestsOverflow[key];
    }
}
```

The `requests` array has only 32 slots (`NUM_REQUESTS = 32`), so after the first 32 iterations the main-array SLOADs are warm (~100 gas each), but every `requestsOverflow[key]` lookup uses a unique mapping key — a cold SLOAD at ~2,100 gas each. At ~2,200 gas per iteration and a 30 M gas block limit, the loop can run at most ~13,600 iterations before the transaction runs out of gas.

Because the while loop executes **before** the `try/catch` callback and **after** `clearRequest`, an out-of-gas revert rolls back the entire transaction (including `clearRequest`), leaving the earliest request permanently active but uncallable. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

- The earliest unfulfilled request's `_echoCallback` can never be executed once the while loop cost exceeds the block gas limit.
- Because there is no cancellation or refund path in `Echo.sol`, the requester's fee (stored in `req.fee`) is permanently locked in the contract.
- The provider assigned to that request also permanently loses the fee they were owed.
- The `firstUnfulfilledSeq` pointer is never advanced, so `getFirstActiveRequests` will always return the stuck request as the first result, degrading keeper tooling. [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

An attacker who registers as a provider controls both sides of the interaction:

1. They pay the request fee when creating each request.
2. They earn that same fee back when they call `executeCallback` for requests 2 … N (as the assigned provider within the exclusivity period, using valid Hermes price data for the chosen `publishTime`).

The net cost to the attacker is therefore only the gas cost of N create + N−1 fulfill transactions, not the protocol fees. With ~13,600 iterations needed to hit the block gas limit, and each `requestPriceUpdatesWithCallback` + `executeCallback` pair costing on the order of a few hundred thousand gas, the total attack gas cost is on the order of a few billion gas units — expensive but achievable on a low-fee chain or during a low-congestion period. The `requestsOverflow` mapping is unbounded, so there is no protocol-level cap preventing N from reaching the required threshold. [6](#0-5) [7](#0-6) 

---

### Recommendation

Replace the linear scan with a data structure that allows O(1) advancement of `firstUnfulfilledSeq`, as the developer comment already suggests. A doubly-linked list of active sequence numbers stored in the contract state would allow `firstUnfulfilledSeq` to jump directly to the next active request without scanning every intermediate slot. Alternatively, maintain a sorted min-heap or a bitmap of active slots.

---

### Proof of Concept

```
1. Attacker calls registerProvider(...) to become a registered provider.

2. Attacker calls requestPriceUpdatesWithCallback(attackerAddress, publishTime, priceIds, gasLimit)
   N times (e.g., N = 14,000), paying the required fee each time.
   Sequence numbers 1 … N are assigned. firstUnfulfilledSeq = 0.

3. For sequence numbers 2 … N, attacker calls executeCallback(attackerAddress, seqNum, updateData, priceIds)
   within the exclusivity period (attacker is the assigned provider).
   Each call: the while loop checks firstUnfulfilledSeq (= 0 or 1) → request 1 is still active → loop exits immediately (0–1 iterations). No gas problem.
   After each call, firstUnfulfilledSeq remains at 0 (request 0 never existed, request 1 still active).

4. Attacker calls executeCallback(attackerAddress, 1, updateData, priceIds) for sequence number 1.
   - clearRequest(1) marks slot as cleared.
   - while loop starts at firstUnfulfilledSeq = 0:
       seq 0: findRequest(0) → not active → firstUnfulfilledSeq++
       seq 1: findRequest(1) → just cleared, not active → firstUnfulfilledSeq++
       seq 2: findRequest(2) → fulfilled earlier, not active → firstUnfulfilledSeq++
       ...
       seq N: findRequest(N) → fulfilled earlier, not active → firstUnfulfilledSeq++
       seq N+1: N+1 == currentSequenceNumber → loop exits
   - Total iterations: N+1 ≈ 14,000 → OUT OF GAS → entire transaction reverts.
   - clearRequest(1) is also reverted → request 1 is still active but permanently uncallable.

5. Requester's fee in request 1 is permanently locked. No refund path exists.
``` [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-102)
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

        Request storage req = allocRequest(requestSequenceNumber);
        req.sequenceNumber = requestSequenceNumber;
        req.publishTime = publishTime;
        req.callbackGasLimit = callbackGasLimit;
        req.requester = msg.sender;
        req.provider = provider;
        req.fee = SafeCast.toUint96(msg.value - _state.pythFeeInWei);

        // Create array with the right size
        req.priceIdPrefixes = new bytes8[](priceIds.length);

        // Copy only the first 8 bytes of each price ID to storage
        for (uint8 i = 0; i < priceIds.length; i++) {
            // Extract first 8 bytes of the price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }
            req.priceIdPrefixes[i] = prefix;
        }
        _state.accruedFeesInWei += _state.pythFeeInWei;

        emit PriceUpdateRequested(req, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-202)
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

        // Verify priceIds match
        require(
            priceIds.length == req.priceIdPrefixes.length,
            "Price IDs length mismatch"
        );
        for (uint8 i = 0; i < req.priceIdPrefixes.length; i++) {
            // Extract first 8 bytes of the provided price ID
            bytes32 priceId = priceIds[i];
            bytes8 prefix;
            assembly {
                prefix := priceId
            }

            // Compare with stored prefix
            if (prefix != req.priceIdPrefixes[i]) {
                // Now we can directly use the bytes8 prefix in the error
                revert InvalidPriceIds(priceIds[i], req.priceIdPrefixes[i]);
            }
        }

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
        {
            // Callback succeeded
            emitPriceUpdate(sequenceNumber, priceIds, priceFeeds);
        } catch Error(string memory reason) {
            // Explicit revert/require
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                reason
            );
        } catch {
            // Out of gas or other low-level errors
            emit PriceUpdateCallbackFailed(
                sequenceNumber,
                providerToCredit,
                priceIds,
                req.requester,
                "low-level error (possibly out of gas)"
            );
        }
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L466-498)
```text
    function getFirstActiveRequests(
        uint256 count
    )
        external
        view
        override
        returns (Request[] memory requests, uint256 actualCount)
    {
        requests = new Request[](count);
        actualCount = 0;

        // Start from the first unfulfilled sequence and work forwards
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

        // If we found fewer requests than asked for, resize the array
        if (actualCount < count) {
            assembly {
                mstore(requests, actualCount)
            }
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
