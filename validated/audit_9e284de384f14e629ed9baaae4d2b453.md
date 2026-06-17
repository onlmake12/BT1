### Title
`getPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp Instead of Per-Feed `publishTime`, Allowing Stale Prices to Pass Freshness Guard - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Pyth Pulse `Scheduler.sol` contract's `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` functions validate freshness using a single subscription-level `priceLastUpdatedAt` value, which is set to the **maximum** `publishTime` across all feeds in the batch. When a subscription contains feeds for both actively-traded assets (fresh timestamps) and closed-market assets (stale timestamps from the last trading period), the staleness guard passes based on the fresh feed's timestamp while returning arbitrarily old prices for the closed-market feeds. Consumers of `getPricesNoOlderThan` receive a false freshness guarantee.

---

### Finding Description

In `Scheduler.sol`, `updatePriceFeeds` computes `priceLastUpdatedAt` as the maximum `publishTime` across all feeds in the submitted batch:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
// ...
status.priceLastUpdatedAt = latestPublishTime;
``` [1](#0-0) [2](#0-1) 

The contract explicitly documents and tests this behavior — a batch containing one feed with a stale timestamp (closed market) and one with a fresh timestamp is accepted, with `priceLastUpdatedAt` set to the fresh one: [3](#0-2) [4](#0-3) 

The test `testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid` confirms this: a feed with `stalePublishTime` (over 1 hour old) and a feed with `validPublishTime` (30 minutes old) are both stored, and `priceLastUpdatedAt` is set to `validPublishTime`. [5](#0-4) 

However, `getPricesNoOlderThan` checks only `priceLastUpdatedAt` (the max) against `age_seconds`:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [6](#0-5) 

The same flaw exists in `getEmaPricesNoOlderThan`: [7](#0-6) 

The function name `getPricesNoOlderThan` and its NatSpec documentation state it returns prices "no older than `age` seconds." This guarantee is false for any feed in the subscription whose individual `publishTime` is older than `age_seconds`, because the check is against the subscription-level max timestamp, not each feed's own `publishTime`. [8](#0-7) [9](#0-8) 

---

### Impact Explanation

A protocol integrating with Pyth Pulse that calls `getPricesNoOlderThan(subscriptionId, priceIds, 60)` to enforce a 60-second freshness window will receive prices that pass the staleness check but may include individual feed prices that are hours or days old (e.g., a stock or commodity feed from the last trading session). This enables:

- **Stale-price arbitrage**: A user can exploit the gap between the stale on-chain price and the true current price for collateral valuation, liquidations, or derivative settlement.
- **False freshness guarantee**: The function's documented contract is violated — integrators who rely on `getPricesNoOlderThan` as a safety guard are silently exposed to stale data.

This is directly analogous to the original report's finding: a push-type oracle (closed-market feed) is not always updated, and the lack of per-feed staleness validation creates arbitrage opportunities for redemptions and debt operations.

---

### Likelihood Explanation

- The Scheduler is explicitly designed to support mixed subscriptions containing both crypto feeds (always active) and traditional-market feeds (closed on weekends/nights).
- Any keeper can call `updatePriceFeeds` permissionlessly with a batch that includes a fresh crypto feed and a stale closed-market feed, advancing `priceLastUpdatedAt` to the fresh timestamp.
- Protocols that use `getPricesNoOlderThan` as their sole freshness guard — a natural and documented usage pattern — are affected without any additional attacker action.
- The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` constant allows feeds up to 1 hour old to be stored, meaning the stale price window can be up to 1 hour (or longer for weekend-closed markets). [10](#0-9) 

---

### Recommendation

`getPricesNoOlderThan` should validate each individual feed's `publishTime` against `age_seconds`, not the subscription-level `priceLastUpdatedAt`. Replace the current check with a per-feed loop:

```solidity
function getPricesNoOlderThan(...) external view ... {
    PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
    prices = new PythStructs.Price[](priceFeeds.length);
    for (uint i = 0; i < priceFeeds.length; i++) {
        if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
            revert PythErrors.StalePrice();
        prices[i] = priceFeeds[i].price;
    }
}
```

Alternatively, document clearly that `getPricesNoOlderThan` only guarantees that the **most recent feed** in the subscription is fresh, and that individual feed `publishTime` values must be checked separately for closed-market assets.

---

### Proof of Concept

1. Create a Pulse subscription with two price feeds: `CRYPTO/USD` (always active) and `STOCK/USD` (closed market).
2. A keeper calls `updatePriceFeeds` with:
   - `CRYPTO/USD` at `publishTime = block.timestamp` (fresh)
   - `STOCK/USD` at `publishTime = block.timestamp - 8 hours` (last trading session, within `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`)
3. `priceLastUpdatedAt` is set to `block.timestamp` (the max).
4. A consumer protocol calls `getPricesNoOlderThan(subscriptionId, [CRYPTO_ID, STOCK_ID], 60)`.
5. The check `distance(block.timestamp, priceLastUpdatedAt) > 60` evaluates to `distance(T, T) = 0 > 60` → **false**, so no revert.
6. `getPricesUnsafe` returns both prices, including `STOCK/USD` with `publishTime = block.timestamp - 8 hours`.
7. The consumer protocol uses the 8-hour-old stock price believing it is at most 60 seconds old, enabling price-discrepancy exploitation. [11](#0-10) [12](#0-11) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L299-304)
```text
        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L356-371)
```text
    function _validateShouldUpdatePrices(
        uint256 subscriptionId,
        SchedulerStructs.SubscriptionParams storage params,
        SchedulerStructs.SubscriptionStatus storage status,
        PythStructs.PriceFeed[] memory priceFeeds
    ) internal view returns (uint256) {
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L373-386)
```text
        // Calculate the minimum acceptable timestamp (clamped at 0)
        // The maximum acceptable timestamp is enforced by the parsePriceFeedUpdatesWithSlots call
        uint256 minAllowedTimestamp = (block.timestamp >
            PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            : 0;

        // Validate that the update timestamp is not too old
        if (updateTimestamp < minAllowedTimestamp) {
            revert SchedulerErrors.TimestampTooOld(
                updateTimestamp,
                block.timestamp
            );
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L589-597)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
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
