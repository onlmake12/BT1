### Title
Echo Contract Request Queue DoS via Zero `callbackGasLimit` Spam - (`target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

The `Echo` contract's `requestPriceUpdatesWithCallback` is permissionless and accepts a user-controlled `callbackGasLimit` parameter. When set to `0`, the gas-cost component of the fee is entirely eliminated, allowing an attacker to post thousands of requests at minimal cost. Providers will never profitably execute these requests, so they accumulate indefinitely. The off-chain keeper's `getFirstActiveRequests` view function must perform an unbounded O(N) linear scan through all accumulated spam requests to find legitimate ones, degrading provider performance and delaying legitimate price-update callbacks.

---

### Finding Description

**Fee calculation with zero `callbackGasLimit`:**

In `Echo.getFee`, the total fee is:

```
feeAmount = pythFeeInWei + baseFeeInWei + (priceIds.length * feePerFeedInWei) + (callbackGasLimit * feePerGasInWei)
``` [1](#0-0) 

When `callbackGasLimit = 0`, the last term is `0 * feePerGasInWei = 0`. The attacker pays only the fixed base fees — no gas-cost component at all. There is no minimum `callbackGasLimit` enforcement anywhere in `requestPriceUpdatesWithCallback`. [2](#0-1) 

**Requests never get executed:**

When `executeCallback` is called, the callback is invoked with exactly `req.callbackGasLimit` gas:

```solidity
IEchoConsumer(req.requester)._echoCallback{
    gas: req.callbackGasLimit
}(sequenceNumber, priceFeeds)
``` [3](#0-2) 

With `callbackGasLimit = 0`, the callback immediately runs out of gas. More importantly, providers must still pay the full gas cost of calling `executeCallback` (which includes Pyth price feed parsing), but the fee they receive covers zero gas. Rational providers will never execute these requests.

**`firstUnfulfilledSeq` never advances:**

`firstUnfulfilledSeq` only advances inside `executeCallback`, after a request is cleared:

```solidity
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [4](#0-3) 

Since spam requests are never executed, `firstUnfulfilledSeq` stays pinned at the first spam request's sequence number.

**`getFirstActiveRequests` scan is O(N) over spam:**

The off-chain keeper calls `getFirstActiveRequests` to discover pending work. It scans linearly from `firstUnfulfilledSeq` to `currentSequenceNumber`:

```solidity
uint64 currentSeq = _state.firstUnfulfilledSeq;
while (actualCount < count && currentSeq < _state.currentSequenceNumber) {
    Request memory req = findRequest(currentSeq);
    if (isActive(req)) { ... }
    currentSeq++;
}
``` [5](#0-4) 

The `IEcho.sol` NatSpec explicitly acknowledges this cost: *"Each iteration costs approximately 2100 gas for cold storage reads."* [6](#0-5) 

With N spam requests, the keeper must perform N cold SLOAD operations (~2100 gas each) before finding any legitimate request. At 10,000 spam requests, this is ~21M gas per `getFirstActiveRequests` call — exceeding block gas limits on many chains, making the function completely unusable.

**The developers were aware of a related DoS vector but the mitigation is incomplete:**

The `publishTime <= block.timestamp + 60` check was added to prevent a different DoS (far-future requests at low gas price). The inline comment even notes: *"FIXME: this comment is wrong. (we're not using tx.gasprice)"* — confirming the intended mitigation was never fully implemented. [7](#0-6) 

The 60-second limit does not prevent the `callbackGasLimit = 0` attack, since spam requests use `publishTime = block.timestamp` (current time, always valid).

**Overflow mapping bloat:**

The contract stores at most 32 requests in a fixed array (`NUM_REQUESTS = 32`). When more than 32 concurrent requests exist, prior requests are evicted to `requestsOverflow` mapping. [8](#0-7) 

Spam requests filling the 32-slot array force legitimate requests into the more expensive overflow mapping, increasing gas costs for legitimate users. [9](#0-8) 

---

### Impact Explanation

1. **Off-chain keeper DoS**: `getFirstActiveRequests` becomes an unbounded O(N) scan. With enough spam, it exceeds RPC timeout limits or block gas limits, making it impossible for providers to discover legitimate pending requests.
2. **Legitimate request delays**: Legitimate price-update callbacks are never discovered or executed by providers, causing them to expire unfulfilled.
3. **Overflow mapping gas inflation**: Legitimate requests are pushed to the expensive overflow mapping, increasing gas costs for honest users.

---

### Likelihood Explanation

- `requestPriceUpdatesWithCallback` is fully permissionless — any EOA or contract can call it.
- The attacker only pays `pythFeeInWei + baseFeeInWei + (1 * feePerFeedInWei)` per spam request (no gas component). On chains where these base fees are small, the cost to create 10,000 spam requests is negligible.
- No rate limiting, no minimum `callbackGasLimit`, no spam detection exists.
- The attack is economically rational: the attacker pays a small fixed fee per request and permanently degrades the keeper's ability to serve legitimate users.

---

### Recommendation

1. **Enforce a minimum `callbackGasLimit`**: Add a check in `requestPriceUpdatesWithCallback` requiring `callbackGasLimit >= MIN_CALLBACK_GAS_LIMIT` (e.g., 21,000 or the provider's configured minimum).
2. **Require non-zero `feePerGasInWei`**: Providers should be required to set a non-zero `feePerGasInWei` so that any non-trivial `callbackGasLimit` results in a meaningful fee.
3. **Cap `currentSequenceNumber - firstUnfulfilledSeq`**: Reject new requests if the gap between `firstUnfulfilledSeq` and `currentSequenceNumber` exceeds a configurable maximum, preventing unbounded queue growth.

---

### Proof of Concept

```solidity
// Attacker contract
contract EchoSpammer {
    IEcho echo;
    address provider;

    constructor(address _echo, address _provider) {
        echo = IEcho(_echo);
        provider = _provider;
    }

    // Spam 1000 requests with callbackGasLimit = 0
    // Fee = pythFeeInWei + baseFeeInWei + feePerFeedInWei (no gas component)
    function spam() external payable {
        bytes32[] memory priceIds = new bytes32[](1);
        priceIds[0] = bytes32(uint256(1));

        uint96 minFee = echo.getFee(provider, 0, priceIds); // callbackGasLimit = 0

        for (uint i = 0; i < 1000; i++) {
            echo.requestPriceUpdatesWithCallback{value: minFee}(
                provider,
                uint64(block.timestamp), // valid publishTime
                priceIds,
                0  // callbackGasLimit = 0 → no gas fee component
            );
        }
        // After this: firstUnfulfilledSeq is stuck, getFirstActiveRequests
        // must scan 1000 slots before finding any legitimate request.
    }
}
```

After `spam()` executes, `getFirstActiveRequests(10)` must iterate through all 1000 spam sequence numbers (each requiring a cold SLOAD at ~2100 gas) before returning results, costing ~2.1M gas per call. Scaling to 10,000 spam requests makes the function exceed block gas limits entirely.

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L169-174)
```text
        while (
            _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
            !isActive(findRequest(_state.firstUnfulfilledSeq))
        ) {
            _state.firstUnfulfilledSeq++;
        }
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L176-179)
```text
        try
            IEchoConsumer(req.requester)._echoCallback{
                gas: req.callbackGasLimit
            }(sequenceNumber, priceFeeds)
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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L477-490)
```text
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

**File:** target_chains/ethereum/contracts/contracts/echo/EchoState.sol (L6-7)
```text
    uint8 public constant NUM_REQUESTS = 32;
    bytes1 public constant NUM_REQUESTS_MASK = 0x1f;
```
