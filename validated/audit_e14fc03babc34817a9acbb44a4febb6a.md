### Title
Deviation-Only Scheduler Subscriptions Permanently Frozen When Any Price Reaches Zero — (File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol)

### Summary

In `Scheduler._validateShouldUpdatePrices`, a zero-price guard silently skips all deviation calculations for any feed whose current or previous stored price is `0`. When a subscription is configured with `updateOnDeviation: true` and `updateOnHeartbeat: false`, and every feed in the subscription has a zero current price, the deviation loop exhausts without returning, causing `updatePriceFeeds` to always revert with `UpdateConditionsNotMet`. The subscription's stored prices are permanently frozen for the duration of the zero-price condition, and any downstream consumer calling `getPricesNoOlderThan` will receive `StalePrice` reverts.

---

### Finding Description

`Scheduler.updatePriceFeeds` delegates trigger validation to `_validateShouldUpdatePrices`. Inside that function, the deviation branch iterates over all price feeds and contains the following guard:

```solidity
// Skip if either price is zero to avoid division by zero
if (previousPrice == 0 || currentPrice == 0) {
    continue;
}
``` [1](#0-0) 

If every feed in the subscription satisfies this condition (i.e., every `currentPrice` is `0`), the loop completes without ever executing `return updateTimestamp`, and the function falls through to `revert SchedulerErrors.UpdateConditionsNotMet()`. [2](#0-1) 

The heartbeat path is the only escape hatch, but it is disabled for subscriptions configured with `updateOnHeartbeat: false`:

```solidity
if (params.updateCriteria.updateOnHeartbeat) { ... }   // skipped
if (params.updateCriteria.updateOnDeviation)  { ... }   // all-continue → falls through
// implicit: revert UpdateConditionsNotMet
``` [3](#0-2) 

The `parsePriceFeedUpdatesWithConfig` call that precedes this check uses `minPublishTime = 0`, so it successfully parses any valid update data regardless of how old it is. The freeze is therefore not caused by the Pyth core parser — it is caused exclusively by the Scheduler's own deviation logic silently discarding zero-valued prices. [4](#0-3) 

Once the subscription is frozen, `getPricesNoOlderThan` (and `getEmaPricesNoOlderThan`) will revert with `StalePrice` for any caller whose `age_seconds` threshold is exceeded:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
``` [5](#0-4) 

---

### Impact Explanation

Any protocol that reads prices from a Scheduler subscription via `getPricesNoOlderThan` will receive `StalePrice` reverts for the entire duration that the affected feed's price is zero. Operations that depend on those prices — including collateral valuation, liquidation checks, and settlement — are frozen at exactly the moment they are most needed (during a market crash or oracle disruption). This is the direct on-chain analog of the Blueberry M-6 finding: the critical consumer operation reverts because the price-read path reverts.

---

### Likelihood Explanation

A Pyth price feed's `int64 price` field can legitimately reach `0` in two realistic scenarios:

1. **Extreme market event**: An asset's price collapses to a value that, after applying the feed's negative exponent, rounds to zero in the `int64` representation stored on-chain.
2. **Oracle disruption / feed misconfiguration**: A publisher submits a zero price, or the aggregation produces a zero aggregate during a data gap.

Both scenarios are low-probability individually, but they are precisely the conditions under which the freeze is most harmful. A subscription owner has no on-chain recourse: they cannot add a heartbeat criterion to an existing subscription without redeploying, and the keeper cannot force an update through any other path.

---

### Recommendation

Replace the silent `continue` with a positive trigger: if either price is zero, treat the feed as having undergone an infinite deviation and immediately return `updateTimestamp`, rather than skipping it:

```solidity
if (previousPrice == 0 || currentPrice == 0) {
    // Treat a zero price as a maximum-deviation event; always update.
    return updateTimestamp;
}
```

Alternatively, add a mandatory heartbeat fallback so that no subscription can be configured with deviation-only criteria, ensuring there is always a time-based escape hatch.

---

### Proof of Concept

1. Deploy `Scheduler` and create a subscription with:
   - `updateOnHeartbeat: false`
   - `updateOnDeviation: true`, `deviationThresholdBps: 100`
   - Two price IDs.
2. Perform a first `updatePriceFeeds` call with both prices set to `1000`. This succeeds (first-update path: `previousFeed.id == bytes32(0)`).
3. Advance time by 10 seconds. Prepare a second update with both prices set to `0`.
4. Call `updatePriceFeeds`. Observe revert `UpdateConditionsNotMet` — the deviation loop hits `continue` for both feeds (because `currentPrice == 0`) and falls through.
5. Advance time by 1 hour. Prepare a third update with both prices set to `0` again.
6. Call `updatePriceFeeds`. Same revert — the subscription is permanently frozen while prices remain zero.
7. Call `getPricesNoOlderThan(subscriptionId, priceIds, 60)`. Observe revert `StalePrice` — downstream consumers are now blocked. [6](#0-5) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L399-451)
```text
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

        // If updateOnDeviation is enabled, check if any price has deviated enough
        if (params.updateCriteria.updateOnDeviation) {
            for (uint8 i = 0; i < priceFeeds.length; i++) {
                // Get the previous price feed for this price ID using subscriptionId
                PythStructs.PriceFeed storage previousFeed = _state
                    .priceUpdates[subscriptionId][priceFeeds[i].id];

                // If there's no previous price, this is the first update
                if (previousFeed.id == bytes32(0)) {
                    return updateTimestamp;
                }

                // Calculate the deviation percentage
                int64 currentPrice = priceFeeds[i].price.price;
                int64 previousPrice = previousFeed.price.price;

                // Skip if either price is zero to avoid division by zero
                if (previousPrice == 0 || currentPrice == 0) {
                    continue;
                }

                // Calculate absolute deviation basis points (scaled by 1e4)
                uint256 numerator = SignedMath.abs(
                    currentPrice - previousPrice
                );
                uint256 denominator = SignedMath.abs(previousPrice);
                uint256 deviationBps = Math.mulDiv(
                    numerator,
                    10_000,
                    denominator
                );

                // If deviation exceeds threshold, trigger update
                if (
                    deviationBps >= params.updateCriteria.deviationThresholdBps
                ) {
                    return updateTimestamp;
                }
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L549-553)
```text
        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

```
