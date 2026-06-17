### Title
Stale Per-Feed Price Data Returned by `getPricesNoOlderThan` Due to Subscription-Level Timestamp Check — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler::getPricesNoOlderThan` validates freshness using `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all price feeds in the last update batch. When a subscription contains a mix of active-market feeds (fresh timestamps) and closed-market feeds (stale timestamps from the last trading period), the subscription-level check passes because the max timestamp is fresh, but the function then returns the stale per-feed data unconditionally. A whitelisted reader calling `getPricesNoOlderThan` with a tight age threshold (e.g., 60 seconds) silently receives prices that are hours or days old for closed-market feeds.

---

### Finding Description

In `updatePriceFeeds`, the Scheduler calls `parsePriceFeedUpdatesWithConfig` with `minPublishTime = 0`, explicitly allowing any per-feed timestamp: [1](#0-0) 

`_validateShouldUpdatePrices` then computes `updateTimestamp` as the **maximum** `publishTime` across all feeds: [2](#0-1) 

The staleness guard only checks this aggregate maximum against `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`. If even one feed has a fresh timestamp, the entire batch passes: [3](#0-2) 

`_storePriceUpdates` then stores **all** feeds unconditionally, including those with arbitrarily old `publishTime` values: [4](#0-3) 

`priceLastUpdatedAt` is set to the max timestamp (fresh), not to individual feed timestamps: [5](#0-4) 

Finally, `getPricesNoOlderThan` checks only `priceLastUpdatedAt` (the max) against `age_seconds`, then returns all requested prices via `getPricesUnsafe` without any per-feed timestamp validation: [6](#0-5) 

The result: a caller requesting "prices no older than 60 seconds" may receive a price with a `publishTime` from days ago for a closed-market feed, with no revert or warning.

---

### Impact Explanation

Any protocol that integrates with the Scheduler and calls `getPricesNoOlderThan` to enforce price freshness before executing financial logic (liquidations, collateral valuation, settlement) will silently receive stale per-feed prices whenever the subscription includes a closed-market asset alongside an active one. The function's name and signature create a false guarantee of freshness. Downstream protocols acting on the stale price could execute incorrect liquidations, mispriced settlements, or incorrect collateral valuations.

---

### Likelihood Explanation

Multi-asset subscriptions mixing equities (closed outside trading hours) with crypto (always active) are a primary use case for the Scheduler. Any keeper can call `updatePriceFeeds` with no access control — it is a fully open `external` function. The Pyth oracle legitimately produces stale timestamps for closed markets in every update slot. No key compromise or privileged access is required; a keeper simply submits valid Pyth update data containing a closed-market feed alongside a fresh one.

---

### Recommendation

`getPricesNoOlderThan` should validate each individual feed's `publishTime` against `age_seconds`, not the subscription-level `priceLastUpdatedAt`. For example:

```solidity
function getPricesNoOlderThan(
    uint256 subscriptionId,
    bytes32[] calldata priceIds,
    uint256 age_seconds
) external view override onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    prices = this.getPricesUnsafe(subscriptionId, priceIds);
    for (uint i = 0; i < prices.length; i++) {
        if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
            revert PythErrors.StalePrice();
    }
}
```

Alternatively, document clearly that `getPricesNoOlderThan` only guarantees that the **most recent feed** in the batch is fresh, and that callers must check individual `publishTime` fields for per-feed freshness.

---

### Proof of Concept

1. Create a Scheduler subscription with two price IDs: `AAPL/USD` (equity, closed market) and `BTC/USD` (crypto, always active).
2. As an unprivileged keeper, call `updatePriceFeeds` with valid Pyth update data from the latest Pythnet slot. `BTC/USD` has `publishTime = block.timestamp`. `AAPL/USD` has `publishTime = block.timestamp - 86400` (last trading close, 24 hours ago).
3. The slot check passes (same Pythnet slot). `_validateShouldUpdatePrices` computes `updateTimestamp = max(now, now-86400) = now` → passes `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check. Both feeds are stored. `priceLastUpdatedAt = now`.
4. A whitelisted reader calls `getPricesNoOlderThan(subscriptionId, [AAPL_id], 60)`.
5. The check `distance(block.timestamp, priceLastUpdatedAt) = distance(now, now) = 0 ≤ 60` passes.
6. `getPricesUnsafe` returns `AAPL/USD` with `publishTime = now - 86400` — 24 hours stale — with no revert.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L311-319)
```text
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L366-371)
```text
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L380-386)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L823-832)
```text
    function _storePriceUpdates(
        uint256 subscriptionId,
        PythStructs.PriceFeed[] memory priceFeeds
    ) internal {
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            _state.priceUpdates[subscriptionId][priceFeeds[i].id] = priceFeeds[
                i
            ];
        }
    }
```
