### Title
Unbounded `firstUnfulfilledSeq` Scan Loop in `executeCallback` Can Be DoSed by Attacker-Inflated Request Queue — (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

---

### Summary

`Echo.sol`'s `executeCallback` contains an unbounded `while` loop that advances `_state.firstUnfulfilledSeq` past all consecutive inactive requests after each fulfillment. An unprivileged attacker can create many requests and fulfill all but the lowest-sequence-number one (out of order), causing the while loop to iterate over the entire inactive range when the first request is finally fulfilled. This can exhaust the block gas limit, permanently preventing that `executeCallback` call from succeeding.

---

### Finding Description

After clearing a fulfilled request, `executeCallback` runs:

```solidity
// Echo.sol lines 169–174
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
```

Each iteration calls `findRequest`, which performs an EVM storage read (`SLOAD`). The number of iterations equals the number of consecutive inactive (already-fulfilled) requests starting from `firstUnfulfilledSeq`. The developers themselves flag this in a TODO comment on line 166: *"I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number."*

The attack path:

1. Attacker registers as a provider with zero fees (permissionless via `registerProvider`).
2. Attacker calls `requestPriceUpdatesWithCallback` N times, creating requests with sequence numbers 1 through N. Cost per request is only `pythFeeInWei` (can be as low as 1 wei in deployments).
3. Attacker calls `executeCallback` for requests 2 through N (providing publicly available Pyth price update data), skipping request 1. After each of these calls, the while loop checks `firstUnfulfilledSeq = 1`, finds it still active, and stops immediately — so `firstUnfulfilledSeq` stays at 1.
4. When the legitimate provider (or anyone) calls `executeCallback` for request 1, `clearRequest(1)` executes, then the while loop iterates from sequence 1 through N — N storage reads — before stopping at `currentSequenceNumber`. [1](#0-0) 

The `requestPriceUpdatesWithCallback` entry point is fully permissionless and only requires paying the fee: [2](#0-1) 

`executeCallback` is also permissionless (anyone can call it with valid Pyth data): [3](#0-2) 

---

### Impact Explanation

If the while loop iterates N times and N is large enough (e.g., ~100,000 requests at ~100 gas/warm SLOAD = ~10M gas), the `executeCallback` transaction for request 1 will exceed the block gas limit and revert. Because `clearRequest` is called before the while loop, the revert undoes the clear, leaving request 1 permanently stuck in an unfulfillable state — the provider cannot deliver the price update callback to the consumer regardless of how much gas they supply, since the loop cost is fixed by the attacker's queue size. Consumer contracts relying on Echo for price updates are permanently denied service for that request.

---

### Likelihood Explanation

The attack is cheap: with `pythFeeInWei = 1 wei` (as seen in test fixtures), creating 100,000 requests costs ~100,000 wei plus gas. The attacker registers as a provider with zero fees and recovers provider-side fees when self-fulfilling requests 2–N. Pyth price update data is publicly available from Hermes, so calling `executeCallback` requires no privileged access. The attack is fully executable by any EOA. [4](#0-3) 

---

### Recommendation

Replace the unbounded linear scan with a data structure that does not require iterating over all inactive entries. The developers already note a doubly-linked list of active requests as the correct fix (line 167 comment). Alternatively, remove the `firstUnfulfilledSeq` advancement from `executeCallback` entirely and maintain it lazily or via a separate, gas-bounded admin function. A simpler short-term mitigation is to cap the number of iterations in the while loop per call (e.g., `uint256 maxAdvance = 256`), preventing unbounded gas consumption. [4](#0-3) 

---

### Proof of Concept

```solidity
// Attacker registers as a provider with zero fees
echo.registerProvider(0, 0, 0);

// Attacker creates N requests (cost: N * pythFeeInWei)
uint256 N = 50_000;
for (uint i = 0; i < N; i++) {
    echo.requestPriceUpdatesWithCallback{value: pythFeeInWei}(
        attackerProvider, block.timestamp, priceIds, callbackGasLimit
    );
}
// Sequence numbers 1..N are now active; firstUnfulfilledSeq = 1

// Attacker fulfills requests 2..N (skipping #1), providing valid Pyth data
// Each call: while loop checks seq=1 (active), stops immediately
for (uint64 seq = 2; seq <= N; seq++) {
    echo.executeCallback{value: pythFee}(attackerProvider, seq, updateData, priceIds);
}
// firstUnfulfilledSeq is still 1; requests 2..N are inactive

// Now provider tries to fulfill request #1 — while loop iterates N times → OOG revert
echo.executeCallback{value: pythFee}(provider, 1, updateData, priceIds); // REVERTS
``` [5](#0-4) [1](#0-0)

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

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L104-121)
```text
    // TODO: does this need to be payable? Any cost paid to Pyth could be taken out of the provider's accrued fees.
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
