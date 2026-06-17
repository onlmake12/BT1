### Title
`getPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp Instead of Per-Feed Publish Time, Allowing Stale Prices to Pass Freshness Validation - (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` functions validate freshness using `status.priceLastUpdatedAt`, which stores the **maximum** `publishTime` across all feeds in the last update batch. When a subscription contains a mix of open-market feeds (always fresh) and closed-market feeds (stale during non-trading hours), the staleness check passes because the open-market feed's recent timestamp dominates, while the closed-market feed's arbitrarily old price is silently returned to the caller.

---

### Finding Description

In `updatePriceFeeds`, the contract records `priceLastUpdatedAt` as the maximum `publishTime` across all feeds in the batch:

```solidity
// _validateShouldUpdatePrices
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
// ...
status.priceLastUpdatedAt = latestPublishTime; // max across all feeds
```

The code comment explicitly acknowledges this design: *"Use the most recent timestamp, as some asset markets may be closed. Closed markets will have a publishTime from their last trading period."* [1](#0-0) 

However, `getPricesNoOlderThan` then validates freshness using this subscription-level max timestamp, not the individual feed's `publishTime`:

```solidity
function getPricesNoOlderThan(...) {
    SchedulerStructs.SubscriptionStatus memory status = _state
        .subscriptionStatuses[subscriptionId];
    if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
        revert PythErrors.StalePrice();
    prices = this.getPricesUnsafe(subscriptionId, priceIds);
}
``` [2](#0-1) 

The same flaw exists in `getEmaPricesNoOlderThan`: [3](#0-2) 

A caller requesting only the closed-market feed (e.g., `priceIds = [AAPL_FEED_ID]`) will have their staleness check evaluated against the BTC/USD publishTime (recent), not AAPL's publishTime (days old). The function returns the stale AAPL price without reverting.

---

### Impact Explanation

Any DeFi protocol that integrates Pyth Pulse and calls `getPricesNoOlderThan` with a tight `age_seconds` window (e.g., 60 seconds) to enforce price freshness will silently receive arbitrarily stale prices for closed-market feeds. This breaks the core freshness guarantee the function name and documentation imply. Downstream consequences include:

- Incorrect liquidation thresholds (using a Friday closing price on a Sunday)
- Mispriced collateral valuations
- Exploitable arbitrage: an attacker who knows a subscription contains a stale closed-market feed can call `getPricesNoOlderThan` to obtain the stale price and exploit any protocol that trusts the freshness guarantee

---

### Likelihood Explanation

Subscriptions mixing open-market feeds (crypto) with closed-market feeds (equities, commodities) are a realistic and documented use case — the contract code itself explicitly accommodates closed-market feeds. Any keeper update during non-trading hours will produce exactly this state. The vulnerability is continuously present whenever such a subscription exists and a closed market is inactive.

---

### Recommendation

Replace the subscription-level `priceLastUpdatedAt` check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed `publishTime` check. After retrieving the price feeds via `getPricesUnsafe`, iterate over the returned feeds and verify each individual feed's `publishTime` is within `age_seconds`:

```solidity
function getPricesNoOlderThan(...) external view ... {
    PythStructs.PriceFeed[] memory feeds = _getPricesInternal(subscriptionId, priceIds);
    for (uint i = 0; i < feeds.length; i++) {
        if (distance(block.timestamp, feeds[i].price.publishTime) > age_seconds)
            revert PythErrors.StalePrice();
    }
    // extract and return prices
}
```

This ensures the freshness guarantee applies to each individual feed, not just the most recently updated one in the subscription.

---

### Proof of Concept

1. Manager creates a subscription with two feeds: `BTC_FEED` (crypto, always active) and `AAPL_FEED` (equity, closed on weekends).
2. On Saturday, a keeper calls `updatePriceFeeds`. BTC/USD has `publishTime = now - 5s`. AAPL/USD has `publishTime = Friday_close` (e.g., 2 days ago). Both are accepted because `parsePriceFeedUpdatesWithConfig` uses `minTimestamp = 0`.
3. `priceLastUpdatedAt` is set to `now - 5s` (the max).
4. A reader protocol calls `getPricesNoOlderThan(subscriptionId, [AAPL_FEED_ID], 60)`.
5. The check: `distance(block.timestamp, priceLastUpdatedAt) = 5 < 60` → **passes**.
6. `getPricesUnsafe` returns the AAPL price with `publishTime` from 2 days ago.
7. The reader protocol receives a 2-day-old AAPL price believing it is at most 60 seconds old. [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-348)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );

        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }

        // Verify that update conditions are met, and that the timestamp
        // is more recent than latest stored update's. Reverts if not.
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L356-410)
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

        // Reject updates if they're older than the latest stored ones
        if (
            status.priceLastUpdatedAt > 0 &&
            updateTimestamp <= status.priceLastUpdatedAt
        ) {
            revert SchedulerErrors.TimestampOlderThanLastUpdate(
                updateTimestamp,
                status.priceLastUpdatedAt
            );
        }

        // If updateOnHeartbeat is enabled and the heartbeat interval has passed, trigger update
        if (params.updateCriteria.updateOnHeartbeat) {
            uint256 lastUpdateTime = status.priceLastUpdatedAt;

            if (
                lastUpdateTime == 0 ||
                updateTimestamp >=
                lastUpdateTime + params.updateCriteria.heartbeatSeconds
            ) {
                return updateTimestamp;
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L578-598)
```text
    function getEmaPricesNoOlderThan(
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

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
    }
```
