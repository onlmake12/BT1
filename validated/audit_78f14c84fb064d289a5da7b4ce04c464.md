### Title
`getPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp Instead of Individual Feed `publishTime` — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `getPricesNoOlderThan` (and `getEmaPricesNoOlderThan`) checks staleness against `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the last update batch. Individual feeds — especially closed-market feeds — can have `publishTime` values far older than `age_seconds`. A whitelisted reader calling `getPricesNoOlderThan` for such a feed receives stale data without any revert, violating the function's freshness guarantee.

---

### Finding Description

**Root cause — wrong variable in the staleness check.**

In `_validateShouldUpdatePrices`, `updateTimestamp` is computed as the **max** `publishTime` across all feeds in the batch:

```solidity
// Scheduler.sol lines 366–371
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

This max value is then stored as the subscription-level timestamp:

```solidity
// Scheduler.sol line 340
status.priceLastUpdatedAt = latestPublishTime;
```

The design comment explains the intent:

> *"Use the most recent timestamp, as some asset markets may be closed. Closed markets will have a publishTime from their last trading period."*

However, `getPricesNoOlderThan` reuses this same subscription-level max timestamp for its per-feed freshness guarantee:

```solidity
// Scheduler.sol lines 546–554
SchedulerStructs.SubscriptionStatus memory status = _state
    .subscriptionStatuses[subscriptionId];

if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

The check is performed on `status.priceLastUpdatedAt` (the max across all feeds) rather than on the individual feed's own `publishTime`. If a subscription contains one active feed (e.g., BTC/USD, `publishTime = now`) and one closed-market feed (e.g., a commodity futures feed, `publishTime = 8 hours ago`), the staleness check passes for both, even when the caller requests only the closed-market feed with `age_seconds = 60`.

The identical bug exists in `getEmaPricesNoOlderThan` at lines 589–597.

---

### Impact Explanation

A whitelisted reader (e.g., a DeFi lending protocol) calls `getPricesNoOlderThan(subscriptionId, [closedMarketFeedId], 60)` expecting a price no older than 60 seconds. The function returns a price whose `publishTime` is hours old without reverting. The protocol proceeds with a stale price, potentially enabling under-collateralised borrowing, incorrect liquidations, or mispriced derivatives.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support mixed subscriptions containing both active and closed-market feeds (the comment in `_validateShouldUpdatePrices` confirms this). Any subscription with at least one active feed and one closed-market feed creates the condition. Any whitelisted reader that queries a closed-market feed via `getPricesNoOlderThan` is affected. The Pyth network already publishes commodity futures feeds (e.g., `Commodities.WTIJ6/USD`) alongside crypto feeds, making mixed subscriptions a realistic and likely deployment pattern.

---

### Recommendation

In `getPricesNoOlderThan` and `getEmaPricesNoOlderThan`, check the staleness of each **individual** returned feed's `publishTime` rather than the subscription-level `priceLastUpdatedAt`:

```solidity
// After fetching prices, verify each feed individually:
for (uint i = 0; i < prices.length; i++) {
    if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

Alternatively, store per-feed `publishTime` in `_state.priceUpdates` and check those directly.

---

### Proof of Concept

1. Create a subscription with two price IDs: `BTC/USD` (active, updates every second) and `WTIJ6/USD` (closed market, last `publishTime` = 8 hours ago).
2. Call `updatePriceFeeds` with a batch containing both feeds. `status.priceLastUpdatedAt` is set to BTC/USD's recent `publishTime`.
3. Call `getPricesNoOlderThan(subscriptionId, [WTIJ6/USD_id], 60)`.
4. The check `distance(block.timestamp, status.priceLastUpdatedAt) > 60` evaluates to `false` (BTC/USD is fresh), so no revert occurs.
5. The returned price for `WTIJ6/USD` has `publishTime` = 8 hours ago — far outside the requested 60-second window.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L340-341)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L546-554)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
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
