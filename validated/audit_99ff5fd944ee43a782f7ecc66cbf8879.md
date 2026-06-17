### Title
Scheduler `getPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp, Allowing Stale Individual Feed Prices to Pass Freshness Validation — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` functions validate freshness using `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the last update batch. When a subscription contains feeds from closed markets (e.g., equities), those feeds carry stale timestamps from their last trading session. A keeper can submit a valid update batch where one feed is fresh (crypto) and another is stale (closed market). The max timestamp passes the `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check, `priceLastUpdatedAt` is set to the fresh feed's timestamp, and subsequently any reader calling `getPricesNoOlderThan` for the stale feed receives a price that is arbitrarily old — while the staleness guard silently passes.

---

### Finding Description

In `_validateShouldUpdatePrices`, the contract computes `updateTimestamp` as the maximum `publishTime` across all feeds in the submitted batch: [1](#0-0) 

This maximum is then stored as the subscription-level `priceLastUpdatedAt`: [2](#0-1) 

The minimum-timestamp validity check (`PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hour`) is also applied only against this maximum: [3](#0-2) [4](#0-3) 

The design explicitly allows stale individual feeds to be stored alongside fresh ones (the comment says "some asset markets may be closed"). The test `testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid` confirms this: a feed with `publishTime` more than 1 hour old is stored successfully as long as another feed in the same batch has a recent timestamp. [5](#0-4) 

However, `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` check only `status.priceLastUpdatedAt` (the subscription-level max), not the individual feed's `publishTime`: [6](#0-5) [7](#0-6) 

The returned `PythStructs.Price` struct contains the individual feed's actual (stale) `publishTime`, but the staleness guard has already passed using the subscription-level max. A reader protocol that trusts `getPricesNoOlderThan` to enforce per-feed freshness receives a price that may be hours or days old.

---

### Impact Explanation

Any protocol that:
1. Creates a Scheduler subscription containing a mix of always-on feeds (crypto) and session-based feeds (equities, FX, commodities), and
2. Calls `getPricesNoOlderThan` or `getEmaPricesNoOlderThan` to enforce freshness before making financial decisions (e.g., collateral valuation, liquidation thresholds, settlement prices),

will silently receive stale prices for the session-based feeds while the staleness check passes. An adversary who knows the market is closed can exploit the stale price to take positions that are profitable against the protocol's stale valuation — directly analogous to the MarginSwap stale-oracle drain.

The Scheduler README explicitly recommends `get(Ema)PricesNoOlderThan` as the safe read path: [8](#0-7) 

This recommendation is misleading for multi-asset subscriptions containing closed-market feeds.

---

### Likelihood Explanation

- Pyth Pulse is designed to support equities and other session-based assets alongside crypto feeds in the same subscription.
- A keeper (any unprivileged actor) can submit a valid Pyth update batch at any time, including during market-closed hours, with the stale equity feed and a fresh crypto feed in the same batch.
- The Pyth contract validates the VAA/slot authenticity; the stale timestamp is genuine (from the last trading session), so no forgery is required.
- The keeper is economically incentivized to submit updates whenever the heartbeat or deviation condition is met — which can be triggered by the fresh crypto feed alone.
- Protocols integrating Pulse for multi-asset subscriptions are the natural target.

---

### Recommendation

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` should validate freshness against each **individual feed's** `publishTime`, not the subscription-level `priceLastUpdatedAt`. Concretely:

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

Alternatively, the function's NatSpec and the Pulse README should prominently document that the staleness guarantee is subscription-level (max timestamp), not per-feed, so integrators can apply their own per-feed `publishTime` checks on the returned structs.

---

### Proof of Concept

1. Manager creates a subscription with two feeds: `CRYPTO_FEED` (always-on) and `EQUITY_FEED` (session-based), with `updateOnHeartbeat = true, heartbeatSeconds = 3600`.
2. At market close, a keeper submits `updatePriceFeeds` with:
   - `CRYPTO_FEED.publishTime = now` (fresh)
   - `EQUITY_FEED.publishTime = now - 8 hours` (last trading session, stale but within `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` is **not** required — only the max needs to be within 1 hour)
3. `_validateShouldUpdatePrices` computes `updateTimestamp = max(now, now-8h) = now`. Validity check passes. `priceLastUpdatedAt = now`.
4. Both feeds are stored. `EQUITY_FEED` price is 8 hours old.
5. Next morning, a protocol calls `getPricesNoOlderThan(subscriptionId, [EQUITY_FEED], 3600)`.
6. Check: `distance(block.timestamp, priceLastUpdatedAt) = distance(now+8h, now) = 8h > 3600s` — **wait**, this would revert.

Correction: the keeper must re-submit every heartbeat. At `now + 1h`, the keeper submits again with `CRYPTO_FEED.publishTime = now+1h` and `EQUITY_FEED.publishTime = now-7h`. `priceLastUpdatedAt = now+1h`. The protocol calls `getPricesNoOlderThan(..., 3600)` at `now+1h+30min`: `distance(now+1h+30min, now+1h) = 30min < 3600s` → **passes**. But `EQUITY_FEED.publishTime = now-7h` → price is 7.5 hours old. The protocol receives a 7.5-hour-old equity price while believing it is at most 1 hour old. [9](#0-8) [10](#0-9)

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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L2322-2371)
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
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L56-57)
```markdown
7.  **Reading:** Readers get prices using the `@pythnetwork/pyth-sdk-solidity` SDK. Readers are recommended to use the SDK's functions `get(Ema)PricesNoOlderThan`, which wrap the contract's `get(Ema)PricesUnsafe` functions and validate that the price is recent.

```
