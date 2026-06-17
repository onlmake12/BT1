### Title
`Echo::executeCallback` Uses Zeroed Storage After `clearRequest` for Overflow Requests, Silently Skipping Consumer Callbacks — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

In `Echo.executeCallback`, `clearRequest(sequenceNumber)` is called before `req.requester` and `req.callbackGasLimit` are accessed for the callback. For requests stored in the overflow mapping (when the 32-slot array is full), `clearRequest` executes `delete _state.requestsOverflow[key]`, zeroing all struct fields. The `req` storage pointer still points to the now-zeroed slot, so the callback is silently made to `address(0)` with 0 gas — appearing to succeed — while the actual consumer never receives their price update.

---

### Finding Description

**Request storage layout:** The Echo contract stores at most `NUM_REQUESTS = 32` concurrent requests in a fixed array `_state.requests[32]`. When the array is full, new requests overflow into `_state.requestsOverflow` (a mapping). [1](#0-0) [2](#0-1) 

**The vulnerable sequence in `executeCallback`:**

```
1. Request storage req = findActiveRequest(sequenceNumber);
   // req points to _state.requestsOverflow[key] for overflow requests

2. _state.providers[providerToCredit].accruedFeesInWei += (req.fee + msg.value) - pythFee;
   // req.fee read correctly here

3. clearRequest(sequenceNumber);
   // For overflow: delete _state.requestsOverflow[key] → ALL fields zeroed
   // req.requester == address(0), req.callbackGasLimit == 0

4. try IEchoConsumer(req.requester)._echoCallback{gas: req.callbackGasLimit}(...)
   // Calls address(0) with gas=0 → silently succeeds (no code at address(0))
   // emitPriceUpdate() fires as if callback succeeded
``` [3](#0-2) 

**`clearRequest` for overflow deletes the entire struct:** [4](#0-3) 

**`findRequest` returns a storage pointer to the overflow mapping slot:** [5](#0-4) 

After `delete _state.requestsOverflow[key]`, the `req` storage pointer in `executeCallback` still references the same slot — now all zeros. `req.requester = address(0)` and `req.callbackGasLimit = 0`.

In Solidity 0.8+, calling `IEchoConsumer(address(0))._echoCallback{gas: 0}(...)` via `try` succeeds silently: `address(0)` has no code, the EVM CALL returns success with empty data, and since `_echoCallback` returns void, the `try` block completes without error. `PriceUpdateExecuted` is emitted as if the callback succeeded.

---

### Impact Explanation

For any request that lands in the overflow mapping:
- The consumer's fee is permanently credited to `providerToCredit` (taken from the user).
- The consumer's `_echoCallback` is never invoked — the price update is never delivered.
- `PriceUpdateExecuted` is emitted misleadingly, making it appear the callback succeeded.
- The request is cleared and cannot be retried.

This constitutes **loss of user funds** (fee taken, service not rendered) and **denial of service** (price update callback permanently lost).

---

### Likelihood Explanation

The 32-slot array fills when 32 requests are concurrently unfulfilled. An unprivileged attacker can deliberately trigger this:

1. Register as a provider (`registerProvider`).
2. Make 32 requests via `requestPriceUpdatesWithCallback` and do not fulfill them (or wait for the exclusivity period to expire so they cannot be fulfilled by the original provider).
3. Any subsequent victim request goes to the overflow mapping.
4. After the exclusivity period expires, call `executeCallback(attackerAddress, victimSeq, ...)`.
5. `clearRequest` deletes the overflow struct; callback goes to `address(0)`; attacker receives the fee.

The cost is 32 × request fee, which is bounded and feasible. In a naturally busy deployment the array may fill without attacker intervention. [6](#0-5) [7](#0-6) 

---

### Recommendation

Save `req.requester` and `req.callbackGasLimit` to local (memory) variables **before** calling `clearRequest`, so the callback uses the original values regardless of storage deletion:

```solidity
address requester = req.requester;
uint32 callbackGasLimit = req.callbackGasLimit;
uint96 storedFee = req.fee;

_state.providers[providerToCredit].accruedFeesInWei += SafeCast
    .toUint128((storedFee + msg.value) - pythFee);

clearRequest(sequenceNumber);

// ... firstUnfulfilledSeq update ...

try IEchoConsumer(requester)._echoCallback{gas: callbackGasLimit}(sequenceNumber, priceFeeds) {
    emitPriceUpdate(sequenceNumber, priceIds, priceFeeds);
} catch Error(string memory reason) { ... }
  catch { ... }
```

---

### Proof of Concept

```solidity
// 1. Attacker fills the 32-slot array
for (uint i = 0; i < 32; i++) {
    echo.requestPriceUpdatesWithCallback{value: fee}(
        attackerProvider, block.timestamp, priceIds, gasLimit
    );
    // Do NOT call executeCallback — leave requests unfulfilled
}

// 2. Victim makes a request → goes to overflow mapping
vm.prank(victim);
uint64 victimSeq = echo.requestPriceUpdatesWithCallback{value: fee}(
    attackerProvider, block.timestamp, priceIds, gasLimit
);

// 3. Wait for exclusivity period to expire (or use attacker as provider)
vm.warp(block.timestamp + exclusivityPeriod + 1);

// 4. Attacker calls executeCallback
echo.executeCallback(attackerAddress, victimSeq, updateData, priceIds);

// Result:
// - clearRequest deletes _state.requestsOverflow[key]
// - req.requester == address(0), req.callbackGasLimit == 0
// - Callback to address(0) silently succeeds
// - PriceUpdateExecuted emitted (misleadingly)
// - Victim's _echoCallback never called
// - Attacker credited victim's fee
assert(echo.getProviderInfo(attackerAddress).accruedFeesInWei > 0);
// Victim's consumer contract: _echoCallback was never invoked
``` [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-10)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
    // Maximum number of price feeds per request. This limit keeps gas costs predictable and reasonable. 10 is a reasonable number for most use cases.
    // Requests with more than 10 price feeds should be split into multiple requests
    uint8 public constant MAX_PRICE_IDS = 10;
```

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L65-68)
```text
        // These take their own slots regardless of ordering
        Request[NUM_REQUESTS] requests;
        mapping(bytes32 => Request) requestsOverflow;
        mapping(address => ProviderInfo) providers;
```

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L161-202)
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
