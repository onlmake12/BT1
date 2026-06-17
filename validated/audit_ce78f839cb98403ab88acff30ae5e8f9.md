### Title
Stale Price Data Bypasses Freshness Check in `getPricesNoOlderThan` / `getEmaPricesNoOlderThan` Due to Max-Timestamp Aggregation — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler._validateShouldUpdatePrices` derives a single `updateTimestamp` by taking the **maximum** `publishTime` across all price feeds in a subscription batch. This value is stored as `status.priceLastUpdatedAt`. The public functions `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` check staleness exclusively against this single max-timestamp, then return **all** feeds — including those whose individual `publishTime` may be hours older. A consumer calling `getPricesNoOlderThan(id, priceIds, 60)` can silently receive prices that are far outside the requested freshness window.

---

### Finding Description

In `_validateShouldUpdatePrices`, the representative timestamp for the entire batch is computed as the maximum across all feeds:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [1](#0-0) 

This value is then stored as the subscription-level freshness indicator:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
``` [2](#0-1) 

Both `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` perform their staleness gate exclusively against this single field:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) [4](#0-3) 

After the gate passes, `getPricesUnsafe` returns the raw stored `PriceFeed` structs, each carrying its own individual `publishTime`. For a closed-market feed, that individual `publishTime` may be many hours in the past, yet the gate never inspects it.

The code explicitly acknowledges that closed-market feeds will carry old timestamps:

> "We don't want to reject update data if it contains a price from a market that closed a few days ago, since it will contain a timestamp from the last trading period." [5](#0-4) 

The "same Pythnet slot" check only verifies that all feeds were packaged in the same Pythnet update cycle — it does **not** bound how old any individual feed's `publishTime` is within that cycle. [6](#0-5) 

The `SubscriptionStatus` struct documents `priceLastUpdatedAt` as representing the freshness of **all** feeds together, which is the false guarantee:

```solidity
uint256 priceLastUpdatedAt; // Timestamp of the last update. All feeds in the subscription are updated together.
``` [7](#0-6) 

---

### Impact Explanation

Any whitelisted reader that calls `getPricesNoOlderThan(subscriptionId, priceIds, age)` or `getEmaPricesNoOlderThan(subscriptionId, priceIds, age)` receives a set of prices that is **not** uniformly bounded by `age`. For a subscription containing one active-market feed (fresh) and one closed-market feed (stale by hours), the staleness gate passes because `priceLastUpdatedAt` equals the fresh feed's timestamp. The returned array silently includes the stale closed-market price.

DeFi protocols that use the Scheduler as a price oracle and rely on `getPricesNoOlderThan` to enforce freshness — e.g., for collateral valuation, liquidation triggers, or settlement — can be made to consume arbitrarily stale prices for closed-market assets without any on-chain revert. This can lead to incorrect liquidations, mispriced collateral, or exploitable arbitrage against the protocol.

**Impact: High** — stale price data is returned through a function whose name and interface explicitly promise freshness.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support subscriptions that mix active-market and closed-market feeds. The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` is 1 hour, meaning a closed-market feed can be up to 1 hour stale before the max-timestamp check rejects the batch — but markets can be closed for much longer (overnight, weekends). Any unprivileged keeper can call `updatePriceFeeds` with a valid VAA containing such a mixed batch; no special privilege is required. [8](#0-7) 

**Likelihood: Medium** — the scenario is a first-class supported use case, not an edge case.

---

### Recommendation

Replace the max-timestamp aggregation in `_validateShouldUpdatePrices` with a **minimum**-timestamp aggregation, consistent with the `DivReducer` fix pattern. The representative timestamp for the batch should reflect the least-fresh feed, not the most-fresh:

```solidity
uint256 updateTimestamp = type(uint256).max;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime < updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

Alternatively, `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` should iterate over each returned feed and check its individual `publishTime` against `age_seconds`, rather than relying on the subscription-level `priceLastUpdatedAt`.

---

### Proof of Concept

1. A subscription is created with two price IDs: `FEED_A` (active equity) and `FEED_B` (closed equity, last traded 6 hours ago).
2. A keeper calls `updatePriceFeeds` with a valid VAA from the current Pythnet slot. `FEED_A.publishTime = now`, `FEED_B.publishTime = now - 6h`. Both share the same slot, so the slot-mismatch check passes.
3. In `_validateShouldUpdatePrices`, `updateTimestamp = max(now, now-6h) = now`. The staleness check `now < minAllowedTimestamp` passes. `status.priceLastUpdatedAt = now`.
4. A whitelisted reader calls `getPricesNoOlderThan(subscriptionId, [FEED_A, FEED_B], 60)`.
5. `distance(block.timestamp, priceLastUpdatedAt) = distance(now, now) = 0 ≤ 60` → gate passes.
6. `getPricesUnsafe` returns both feeds. `FEED_B.price.publishTime = now - 6h` — 6 hours stale — is returned without revert.
7. The calling protocol uses the 6-hour-old `FEED_B` price for a financial decision, believing it is at most 60 seconds old.

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L321-328)
```text
        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L340-340)
```text
        status.priceLastUpdatedAt = latestPublishTime;
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L20-20)
```text
        uint256 priceLastUpdatedAt; // Timestamp of the last update. All feeds in the subscription are updated together.
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```
