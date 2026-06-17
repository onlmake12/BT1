### Title
`priceIds` Array Not Validated for Duplicates or Zero Values in `Echo::requestPriceUpdatesWithCallback` - (File: `target_chains/ethereum/contracts/contracts/echo/Echo.sol`)

### Summary
`Echo::requestPriceUpdatesWithCallback` accepts a `bytes32[] calldata priceIds` array from any unprivileged caller without checking for duplicate entries or zero values (`bytes32(0)`). This is the direct analog of the reported `topHolders` uniqueness bug: an array accepted from an external caller is stored and later used in a critical downstream call without being validated as a unique, non-zero set.

### Finding Description
In `Echo.sol`, `requestPriceUpdatesWithCallback` performs only a maximum-length check on the caller-supplied `priceIds` array:

```solidity
if (priceIds.length > MAX_PRICE_IDS) {
    revert TooManyPriceIds(priceIds.length, MAX_PRICE_IDS);
}
```

No check is made for:
- Duplicate `bytes32` entries (e.g., the same price feed ID repeated)
- Zero values (`bytes32(0)`)

The array is then stored verbatim as `req.priceIdPrefixes` and later forwarded to `pyth.parsePriceFeedUpdates` inside `executeCallback`:

```solidity
PythStructs.PriceFeed[] memory priceFeeds = pyth.parsePriceFeedUpdates{
    value: pythFee
}(
    updateData,
    priceIds,
    SafeCast.toUint64(req.publishTime),
    SafeCast.toUint64(req.publishTime)
);
```

The Pyth oracle's `parsePriceFeedUpdates` requires a valid, non-zero set of price IDs. Passing `bytes32(0)` or duplicate IDs causes this call to revert, making the request permanently unfulfillable.

By contrast, the Scheduler contract — which also accepts a `priceIds` array — explicitly validates both conditions:

```solidity
for (uint i = 0; i < params.priceIds.length; i++) {
    for (uint j = i + 1; j < params.priceIds.length; j++) {
        if (params.priceIds[i] == params.priceIds[j]) {
            revert SchedulerErrors.DuplicatePriceId(params.priceIds[i]);
        }
    }
}
```

Echo has no equivalent guard.

### Impact Explanation
1. **Permanently locked user funds**: A user who submits a request containing `bytes32(0)` or duplicate price IDs creates a request that can never be fulfilled. `executeCallback` will always revert at the `parsePriceFeedUpdates` call. There is no cancellation or refund mechanism in the contract, so the user's fee (`req.fee`) is permanently locked.

2. **Fee accounting corruption**: The fee is computed as `baseFee + feePerFeed * priceIds.length + gasLimit * feePerGas`. With duplicates, the user overpays and the provider is credited for work that was never done (or cannot be done).

3. **`firstUnfulfilledSeq` stall**: The contract advances `_state.firstUnfulfilledSeq` only past fulfilled requests. An unfulfillable request permanently stalls this counter, corrupting the internal sequence tracking for all subsequent requests.

### Likelihood Explanation
The entry point is fully public and unprivileged — any EOA or contract can call `requestPriceUpdatesWithCallback`. No special role, key, or governance access is required. The missing validation is a single missing loop, making accidental or deliberate triggering straightforward.

### Recommendation
Add duplicate and zero-value checks in `requestPriceUpdatesWithCallback` before storing the request, mirroring the pattern already used in `Scheduler._validateSubscriptionParams`:

```solidity
for (uint i = 0; i < priceIds.length; i++) {
    if (priceIds[i] == bytes32(0)) revert InvalidPriceId(priceIds[i], bytes32(0));
    for (uint j = i + 1; j < priceIds.length; j++) {
        if (priceIds[i] == priceIds[j]) revert DuplicatePriceId(priceIds[i]);
    }
}
```

Also add a check for `priceIds.length == 0` to prevent zero-feed requests.

### Proof of Concept
1. Deploy Echo with a registered provider.
2. Call `requestPriceUpdatesWithCallback` with `priceIds = [bytes32(0)]` and sufficient `msg.value`.
3. The request is stored successfully; `_state.accruedFeesInWei` is incremented; the user's fee is held in `req.fee`.
4. Any caller attempts `executeCallback` for this sequence number. `pyth.parsePriceFeedUpdates` reverts because `bytes32(0)` is not a valid price feed ID.
5. `executeCallback` reverts. The request remains active. The user's fee is permanently locked with no recovery path.
6. `_state.firstUnfulfilledSeq` never advances past this sequence number, stalling the counter for all future requests. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L70-98)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/echo/Echo.sol (L144-153)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L175-181)
```text
        for (uint i = 0; i < params.priceIds.length; i++) {
            for (uint j = i + 1; j < params.priceIds.length; j++) {
                if (params.priceIds[i] == params.priceIds[j]) {
                    revert SchedulerErrors.DuplicatePriceId(params.priceIds[i]);
                }
            }
        }
```
