### Title
`getPricesNoOlderThan` Uses Subscription-Level Max Timestamp Instead of Per-Feed Timestamp, Silently Returning Arbitrarily Stale Prices - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Pyth Pulse Scheduler's `getPricesNoOlderThan` (and `getEmaPricesNoOlderThan`) staleness check compares `block.timestamp` against `status.priceLastUpdatedAt`, which is set to the **maximum** `publishTime` across all feeds in the subscription — not the minimum or per-feed value. For subscriptions containing mixed feeds (e.g., a 24/7 crypto feed alongside a closed-market equity/commodity feed), the staleness check passes based on the freshest feed's timestamp, while individual feeds with arbitrarily old `publishTime` values are silently returned. A consumer calling `getPricesNoOlderThan` with a tight `age_seconds` receives a false freshness guarantee and may use stale prices in financial logic, causing fund losses.

---

### Finding Description

**Root cause — `_validateShouldUpdatePrices` stores the max publishTime:**

In `updatePriceFeeds`, after parsing all feeds, `_validateShouldUpdatePrices` computes `updateTimestamp` as the **maximum** `publishTime` across all feeds in the batch:

```solidity
// Use the most recent timestamp, as some asset markets may be closed.
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [1](#0-0) 

The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check (1 hour) is applied only to this **max** timestamp, not to individual feeds:

```solidity
if (updateTimestamp < minAllowedTimestamp) {
    revert SchedulerErrors.TimestampTooOld(...);
}
``` [2](#0-1) [3](#0-2) 

This max timestamp is then stored as the subscription-level `priceLastUpdatedAt`:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
``` [4](#0-3) 

**Root cause — `getPricesNoOlderThan` checks the subscription-level max, not per-feed:**

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [5](#0-4) 

The check uses `priceLastUpdatedAt` (the max publishTime from the last update cycle), not the `publishTime` of the specific feeds being requested. `getPricesUnsafe` then returns the raw stored `PriceFeed` structs, which may carry individual `publishTime` values from days ago for closed-market feeds.

The same flaw exists in `getEmaPricesNoOlderThan`: [6](#0-5) 

**Concrete scenario:**

1. A subscription contains two feeds: `BTC/USD` (24/7 crypto) and `AAPL/USD` (equity, market closed).
2. A keeper calls `updatePriceFeeds`. The Pyth contract returns `BTC/USD.publishTime = now` and `AAPL/USD.publishTime = 8 hours ago` (last trading period). Both share the same Pythnet slot.
3. `updateTimestamp = max(now, 8h ago) = now`. The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check passes. `priceLastUpdatedAt = now`.
4. A consumer calls `getPricesNoOlderThan(subscriptionId, [AAPL_USD_ID], 60)`.
5. Staleness check: `distance(now, now) = 0 ≤ 60` → **passes**.
6. `getPricesUnsafe` returns `AAPL/USD` with `publishTime = 8 hours ago`.
7. The consumer receives a price it believes is ≤60 seconds old, but it is actually 8 hours old.

The code comment in `updatePriceFeeds` explicitly acknowledges that closed-market feeds will carry old timestamps:

> "We don't want to reject update data if it contains a price from a market that closed a few days ago, since it will contain a timestamp from the last trading period." [7](#0-6) 

This design choice for `updatePriceFeeds` is reasonable, but it is not reconciled with the freshness guarantee implied by `getPricesNoOlderThan`.

---

### Impact Explanation

Any protocol that uses `getPricesNoOlderThan` to enforce price freshness before making financial decisions (collateral valuation, liquidation thresholds, settlement prices) will silently receive stale prices for closed-market feeds. The stale price can deviate significantly from the true current price, causing:

- Incorrect collateral valuations leading to under-collateralized positions not being liquidated.
- Incorrect settlement prices causing users to receive more or less than they should.
- Arbitrage opportunities against the protocol at the expense of other users or the protocol treasury.

This is directly analogous to the original report: a price feed with an effectively unbounded staleness window (days, not hours) is used in financial calculations, causing loss of funds.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support mixed-asset subscriptions including closed-market feeds (equities, commodities, FX). Any subscription combining a 24/7 feed with a session-based feed will exhibit this behavior on every non-trading day or outside market hours. The `getPricesNoOlderThan` function is the primary safe API for consumers; its name and signature strongly imply a per-feed freshness guarantee, making it likely that integrating protocols will rely on it without additional per-feed timestamp checks.

---

### Recommendation

Replace the subscription-level `priceLastUpdatedAt` staleness check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed `publishTime` check. After fetching the requested feeds via `_getPricesInternal`, iterate over each returned feed and verify its individual `publishTime`:

```solidity
function getPricesNoOlderThan(
    uint256 subscriptionId,
    bytes32[] calldata priceIds,
    uint256 age_seconds
) external view override onlyWhitelistedReader(subscriptionId) returns (PythStructs.Price[] memory prices) {
    PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
    prices = new PythStructs.Price[](priceFeeds.length);
    for (uint i = 0; i < priceFeeds.length; i++) {
        if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
            revert PythErrors.StalePrice();
        prices[i] = priceFeeds[i].price;
    }
}
```

This ensures the freshness guarantee holds for every individual feed returned, not just the freshest feed in the subscription.

---

### Proof of Concept

```solidity
// Setup: subscription with BTC/USD (crypto, always fresh) + AAPL/USD (equity, closed market)
uint256 subscriptionId = scheduler.createSubscription{value: minBalance}(params);

// Keeper updates: BTC publishTime = now, AAPL publishTime = 8 hours ago (same slot)
// priceLastUpdatedAt = now (max of the two)
scheduler.updatePriceFeeds(subscriptionId, updateData);

// Consumer requests AAPL price with 60-second freshness requirement
bytes32[] memory ids = new bytes32[](1);
ids[0] = AAPL_USD_ID;

// This call SUCCEEDS (does not revert) even though AAPL price is 8 hours old
// because distance(now, priceLastUpdatedAt=now) = 0 <= 60
PythStructs.Price[] memory prices = scheduler.getPricesNoOlderThan(subscriptionId, ids, 60);

// prices[0].publishTime == block.timestamp - 8 hours  ← stale price returned silently
assert(block.timestamp - prices[0].publishTime > 8 hours);
```

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L340-340)
```text
        status.priceLastUpdatedAt = latestPublishTime;
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L551-554)
```text
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L594-597)
```text
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```
