### Title
Unbounded `while` Loop in `executeCallback` Allows Permanent DoS on Request Fulfillment - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary

`Echo.sol`'s `executeCallback` contains an unbounded `while` loop that advances `_state.firstUnfulfilledSeq` past all inactive (fulfilled) requests. Because there is no cap on the number of outstanding requests, an attacker can manufacture a gap of arbitrary size between `firstUnfulfilledSeq` and `currentSequenceNumber`, causing any subsequent `executeCallback` call that closes that gap to consume unbounded gas and potentially exceed the block gas limit, permanently locking a targeted request.

### Finding Description

After clearing a request and before invoking the consumer callback, `executeCallback` runs:

```solidity
while (
    _state.firstUnfulfilledSeq < _state.currentSequenceNumber &&
    !isActive(findRequest(_state.firstUnfulfilledSeq))
) {
    _state.firstUnfulfilledSeq++;
}
``` [1](#0-0) 

Each iteration calls `findRequest`, which performs a `keccak256` hash and at least one cold storage read (`_state.requestsOverflow[key]` for overflow entries). There is no upper bound on how many iterations this loop can execute in a single transaction.

The `requestPriceUpdatesWithCallback` function imposes no limit on the total number of requests that can be created: [2](#0-1) 

The developers themselves flagged this in a TODO comment directly above the loop:

> "I'm pretty sure this is going to use a lot of gas because it's doing a storage lookup for each sequence number. a better solution would be a doubly-linked list of active requests." [3](#0-2) 

### Impact Explanation

An attacker can permanently prevent a specific request from ever being fulfilled:

1. Victim submits request with sequence number `N`.
2. Attacker submits requests `N+1` through `N+M`, paying the required fees.
3. Attacker calls `executeCallback` for each of `N+1 … N+M`, fulfilling them. After each call the loop terminates immediately because `firstUnfulfilledSeq = N` (request `N` is still active).
4. When anyone attempts to fulfill request `N`, `clearRequest(N)` marks it inactive, then the `while` loop must scan all `M` now-inactive slots before stopping. Each iteration costs ≈2,100–5,000 gas (cold storage reads via `findRequest`). At ~30 M block gas limit, M ≈ 6,000–14,000 iterations suffices to exceed the limit.
5. The transaction reverts (all state changes undone), request `N` remains active, and no retry can ever succeed because the loop length is fixed by the on-chain state.

The consumer's callback is never executed, and any ETH locked in the request as fee is permanently inaccessible to the requester.

### Likelihood Explanation

- Entry path is fully unprivileged: `requestPriceUpdatesWithCallback` and `executeCallback` are open to any address.
- The attacker must pay fees for M requests. At low fee settings (provider `baseFeeInWei` / `feePerFeedInWei` / `feePerGasInWei` can be set to zero by a provider), the economic cost can be negligible.
- Sequence numbers are monotonically predictable, so the attacker can target any specific in-flight request.
- The TODO comment confirms the developers are aware of the gas growth but have not mitigated it.

### Recommendation

1. **Remove the inline `while` loop** from `executeCallback`. `firstUnfulfilledSeq` is a convenience hint for `getFirstActiveRequests`; it does not need to be updated atomically on every fulfillment.
2. **Update `firstUnfulfilledSeq` lazily** inside `getFirstActiveRequests` (a view function), or bound the scan to a fixed maximum step count per call.
3. Alternatively, replace the linear scan with a doubly-linked list of active requests (as the TODO already suggests), giving O(1) removal.

### Proof of Concept

```
// Attacker script (pseudocode)
uint64 victimSeq = echo.requestPriceUpdatesWithCallback{value: fee}(...); // seq = N

// Create M requests after the victim's
for (uint i = 0; i < M; i++) {
    attackerSeqs[i] = echo.requestPriceUpdatesWithCallback{value: fee}(...);
}

// Fulfill all attacker requests (loop runs 0 extra iterations each time
// because firstUnfulfilledSeq == N, which is still active)
for (uint i = 0; i < M; i++) {
    echo.executeCallback(provider, attackerSeqs[i], updateData, priceIds);
}

// Now attempt to fulfill the victim's request:
// clearRequest(N) marks it inactive, then the while loop scans N+1 … N+M
// (all inactive) → M iterations → out of gas → revert → permanent DoS
echo.executeCallback(provider, victimSeq, updateData, priceIds); // REVERTS
``` [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L52-73)
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L105-174)
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
```
