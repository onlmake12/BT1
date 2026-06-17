### Title
`Scheduler::getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` Check Subscription-Level Timestamp Instead of Per-Feed `publishTime` — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s staleness guard in `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` validates freshness against `status.priceLastUpdatedAt` — a single subscription-level timestamp equal to the **maximum** `publishTime` across all feeds in the last update batch. Individual feeds whose markets were closed at update time carry their **last-trading-period** `publishTime`, which can be hours or days old. Because the subscription-level timestamp passes the age check, those stale individual feed prices are returned without triggering a revert.

---

### Finding Description

When `updatePriceFeeds()` is called, `_validateShouldUpdatePrices()` computes `updateTimestamp` as the **maximum** `publishTime` across all feeds in the batch:

```solidity
// Scheduler.sol lines 366-371
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

This `updateTimestamp` is then stored as `status.priceLastUpdatedAt`:

```solidity
// Scheduler.sol line 340
status.priceLastUpdatedAt = latestPublishTime;
```

The code explicitly acknowledges that closed-market feeds will have old timestamps:

```solidity
// Scheduler.sol lines 300-304
// Note: We don't want to reject update data if it contains a price
// from a market that closed a few days ago, since it will contain a timestamp
// from the last trading period.
```

When a reader calls `getPricesNoOlderThan()`, the staleness check is:

```solidity
// Scheduler.sol lines 551-552
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
```

This checks only the subscription-level maximum timestamp, **not** the individual `publishTime` of each returned feed. If a subscription contains one actively-traded feed (e.g., BTC/USD, `publishTime = now`) and one closed-market feed (e.g., AAPL/USD, `publishTime = 2 days ago`), the subscription-level timestamp equals `now`, the staleness check passes, and `getPricesNoOlderThan(subscriptionId, [AAPL_ID], 60)` returns a 2-day-old AAPL price without reverting.

The same flaw exists identically in `getEmaPricesNoOlderThan()`:

```solidity
// Scheduler.sol lines 594-595
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
```

---

### Impact Explanation

A whitelisted reader contract that calls `getPricesNoOlderThan(subscriptionId, [closedMarketFeedId], 60)` expecting a price no older than 60 seconds will receive a price that is potentially hours or days old without any revert. The function's name and documented semantics guarantee freshness per the `age_seconds` parameter, but the implementation silently violates this guarantee for any feed whose market was closed at the time of the last update. Downstream protocols relying on this guarantee for liquidation, collateral valuation, or trade execution will operate on stale prices, potentially enabling undercollateralized positions or mispriced trades.

---

### Likelihood Explanation

Pyth Scheduler subscriptions are explicitly designed to support mixed-market subscriptions (the code comment at line 300 acknowledges closed-market feeds). Any subscription containing at least one equity, commodity, or other session-based feed alongside a 24/7 crypto feed will exhibit this behavior during off-hours for the session-based feed. This is a normal, expected subscription configuration, not an edge case. Any keeper can trigger `updatePriceFeeds()` at any time, including during off-market hours, causing the stale individual feed price to be stored and then returned through the falsely-passing staleness check.

---

### Recommendation

The staleness check in `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` must validate the `publishTime` of **each individually requested feed**, not the subscription-level maximum timestamp. Replace the current check with per-feed validation:

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

Apply the same fix to `getEmaPricesNoOlderThan()` using `priceFeeds[i].emaPrice.publishTime`.

---

### Proof of Concept

1. Create a subscription with two price IDs: `BTC_ID` (crypto, 24/7) and `AAPL_ID` (equity, session-based).
2. During US market hours, call `updatePriceFeeds()`. Both feeds have recent `publishTime`. `priceLastUpdatedAt = now`.
3. US market closes. Time advances 18 hours (overnight).
4. Keeper calls `updatePriceFeeds()` again. BTC has `publishTime = now`. AAPL has `publishTime = 18 hours ago` (last trading period). `updateTimestamp = max(now, 18h ago) = now`. `priceLastUpdatedAt = now`.
5. Reader calls `getPricesNoOlderThan(subscriptionId, [AAPL_ID], 60)`.
6. Check: `distance(block.timestamp, priceLastUpdatedAt) = distance(now, now) = 0 ≤ 60` → **passes**.
7. Returns AAPL price with `publishTime = 18 hours ago`. No revert. The caller's 60-second freshness requirement is silently violated. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L299-305)
```text
        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
```

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L546-555)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L589-598)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
    }
```
