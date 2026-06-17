### Title
Subscription-Level Staleness Check in `getPricesNoOlderThan()` Returns Stale Individual Price Feeds - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler.getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` enforce a staleness check against `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the last update batch. However, the functions then return all stored prices via `getPricesUnsafe()` / `getEmaPricesUnsafe()`, which may include individual feeds with `publishTime` values far older than the requested `age_seconds`. Consumers relying on the function's implied per-feed freshness guarantee receive stale prices without any indication.

### Finding Description

In `updatePriceFeeds()`, `status.priceLastUpdatedAt` is set to the maximum `publishTime` across all feeds in the batch:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
// ...
status.priceLastUpdatedAt = latestPublishTime;
```

This is intentional: the Scheduler accepts batches where some feeds (e.g., closed commodity markets) have old `publishTime` values, as long as the **most recent** feed in the batch is within `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` (1 hour). The test `testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid` explicitly confirms this: a batch with one feed at `stalePublishTime` (1 hour 5 minutes ago) and one at `validPublishTime` (30 minutes ago) is accepted, and `priceLastUpdatedAt` is set to `validPublishTime`.

When a consumer then calls `getPricesNoOlderThan(subscriptionId, priceIds, age_seconds)`:

```solidity
function getPricesNoOlderThan(...) external view ... {
    SchedulerStructs.SubscriptionStatus memory status = _state
        .subscriptionStatuses[subscriptionId];

    if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
        revert PythErrors.StalePrice();

    prices = this.getPricesUnsafe(subscriptionId, priceIds);
}
```

The check passes because `priceLastUpdatedAt` (the max timestamp) is within `age_seconds`. But `getPricesUnsafe()` returns all stored feeds, including the one with `stalePublishTime` (1 hour 5 minutes ago). If the consumer passed `age_seconds = 3600` (1 hour), the check passes, but the returned array contains a price that is 1 hour 5 minutes old — older than the requested threshold.

The NatSpec for `getPricesNoOlderThan` in `IScheduler.sol` states: *"Returns the price that is no older than `age` seconds of the current time"* and *"Reverts if the price wasn't updated sufficiently recently"* — implying a per-feed guarantee that is not actually enforced.

### Impact Explanation

Any whitelisted reader of a subscription calling `getPricesNoOlderThan()` with a strict `age_seconds` (e.g., 60 seconds for a DeFi lending protocol) may receive individual price feeds whose `publishTime` is up to `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` (1 hour) old. The function's name and NatSpec imply a per-feed freshness guarantee, so consumers are unlikely to re-check individual `publishTime` values. This can lead to:

- Incorrect collateral valuations in lending protocols
- Incorrect liquidation decisions
- Incorrect margin calculations in derivative protocols

The maximum individual feed staleness is bounded by `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` (1 hour), but this far exceeds the freshness thresholds typical DeFi protocols require (10–60 seconds).

### Likelihood Explanation

This is triggered whenever a subscription contains feeds for assets with different market hours (e.g., a subscription covering both crypto and commodity feeds). The Scheduler explicitly supports this use case and the test `testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid` confirms it is a reachable state. Any whitelisted reader of such a subscription calling `getPricesNoOlderThan()` with a strict `age_seconds` will receive stale individual prices without a revert.

### Recommendation

Change `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` to check each individual feed's `publishTime` against `age_seconds`, rather than the subscription-level `priceLastUpdatedAt`:

```solidity
function getPricesNoOlderThan(...) external view ... {
    PythStructs.Price[] memory prices = this.getPricesUnsafe(subscriptionId, priceIds);
    for (uint i = 0; i < prices.length; i++) {
        if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
            revert PythErrors.StalePrice();
    }
    return prices;
}
```

Alternatively, update the NatSpec to clearly document that the staleness check is subscription-level (using the max timestamp), not per-feed, and that consumers must check individual `publishTime` values.

### Proof of Concept

1. Create a subscription with two price feeds: one crypto feed (always active) and one commodity feed (closed on weekends).
2. On a Monday, call `updatePriceFeeds()` with a batch where the crypto feed has `publishTime = now - 30 minutes` and the commodity feed has `publishTime = now - 65 minutes` (last Friday's close). The update succeeds because `max(publishTime) = now - 30 minutes < PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`.
3. `status.priceLastUpdatedAt` is set to `now - 30 minutes`.
4. A consumer calls `getPricesNoOlderThan(subscriptionId, priceIds, 3600)` (1 hour threshold). The check `distance(block.timestamp, now - 30 minutes) = 30 minutes < 3600` passes.
5. `getPricesUnsafe()` returns both feeds. The commodity feed has `publishTime = now - 65 minutes`, which is older than the requested 3600-second threshold — but no revert occurs.
6. The consumer uses the 65-minute-old commodity price believing it is no older than 1 hour.

This is confirmed by the existing test `testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid` in `target_chains/ethereum/contracts/test/PulseScheduler.t.sol` at lines 2322–2370, which explicitly validates that a batch with a stale feed is accepted when the latest feed is valid. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L362-371)
```text
        // Use the most recent timestamp, as some asset markets may be closed.
        // Closed markets will have a publishTime from their last trading period.
        // Since we verify all updates share the same Pythnet slot, we still ensure
        // that all price feeds are synchronized from the same update cycle.
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L535-555)
```text
    function getPricesNoOlderThan(
        uint256 subscriptionId,
        bytes32[] calldata priceIds,
        uint256 age_seconds
    )
        external
        view
        override
        onlyWhitelistedReader(subscriptionId)
        returns (PythStructs.Price[] memory prices)
    {
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L68-77)
```text
    /// @notice Returns the price that is no older than `age` seconds of the current time.
    /// @dev This function is a sanity-checked version of `getPriceUnsafe` which is useful in
    /// applications that require a sufficiently-recent price. Reverts if the price wasn't updated sufficiently
    /// recently.
    /// @return prices - please read the documentation of PythStructs.Price to understand how to use this safely.
    function getPricesNoOlderThan(
        uint256 subscriptionId,
        bytes32[] calldata priceIds,
        uint256 age
    ) external view returns (PythStructs.Price[] memory prices);
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L2322-2370)
```text
    function testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid() public {
        // Add a subscription and funds
        uint256 subscriptionId = addTestSubscription(
            scheduler,
            address(reader)
        );

        // Advance time past the validity period
        vm.warp(
            block.timestamp +
                scheduler.PAST_TIMESTAMP_MAX_VALIDITY_PERIOD() +
                600
        ); // Warp 1 hour 10 mins

        uint64 currentTime = SafeCast.toUint64(block.timestamp);
        uint64 validPublishTime = currentTime - 1800; // 30 mins ago (within 1 hour validity)
        uint64 stalePublishTime = currentTime -
            (scheduler.PAST_TIMESTAMP_MAX_VALIDITY_PERIOD() + 300); // 1 hour 5 mins ago (outside validity)

        PythStructs.PriceFeed[] memory priceFeeds = new PythStructs.PriceFeed[](
            2
        );
        priceFeeds[0] = createSingleMockPriceFeed(stalePublishTime);
        priceFeeds[1] = createSingleMockPriceFeed(validPublishTime);

        uint64[] memory slots = new uint64[](2);
        slots[0] = 100;
        slots[1] = 100; // Same slot

        // Mock Pyth response (should succeed in the real world as minValidTime is 0)
        mockParsePriceFeedUpdatesWithSlotsStrict(pyth, priceFeeds, slots);
        bytes[] memory updateData = createMockUpdateData(priceFeeds);

        // Expect PricesUpdated event with the latest valid timestamp
        vm.expectEmit();
        emit PricesUpdated(subscriptionId, validPublishTime);

        // Perform update - should succeed because the latest timestamp in the update data is valid
        vm.prank(pusher);
        scheduler.updatePriceFeeds(subscriptionId, updateData);

        // Verify last updated timestamp
        (, SchedulerStructs.SubscriptionStatus memory status) = scheduler
            .getSubscription(subscriptionId);
        assertEq(
            status.priceLastUpdatedAt,
            validPublishTime,
            "Last updated timestamp should be the latest valid one"
        );
```
