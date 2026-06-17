### Title
`getPricesNoOlderThan` Staleness Guard Uses Maximum Timestamp Across All Feeds, Silently Returning Stale Individual Prices — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Pyth Pulse `Scheduler` contract's `getPricesNoOlderThan` (and `getEmaPricesNoOlderThan`) staleness guard checks only the **maximum** publish timestamp stored in `priceLastUpdatedAt` — not each individual feed's timestamp. A subscription containing feeds from different markets (e.g., crypto + equities) will have some feeds with recent timestamps and others with stale timestamps from closed markets. The single-max-timestamp check passes, but the function returns all prices including individually stale ones, with no per-feed freshness signal to the caller.

---

### Finding Description

In `updatePriceFeeds`, after parsing, `_validateShouldUpdatePrices` computes `updateTimestamp` as the **maximum** `publishTime` across all feeds in the batch:

```solidity
// Scheduler.sol lines 366-371
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

This maximum is then stored as the subscription's `priceLastUpdatedAt`:

```solidity
// Scheduler.sol line 340
status.priceLastUpdatedAt = latestPublishTime;
```

The code comment at lines 362–365 explicitly acknowledges that closed markets will have old timestamps, and intentionally uses the max to avoid rejecting those updates. All feeds — including the stale-timestamped ones — are then stored via `_storePriceUpdates`.

When a downstream caller invokes `getPricesNoOlderThan`, the staleness check is:

```solidity
// Scheduler.sol lines 551-554
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

The guard checks only the single stored maximum timestamp. If that maximum is recent (e.g., from a crypto feed updated seconds ago), the check passes and **all** feeds are returned — including equity feeds whose individual `publishTime` may be hours or days old. The caller has no way to distinguish fresh from stale prices in the returned array.

This is structurally identical to the reference bug: a function that is supposed to guarantee price freshness silently returns incorrect (stale) values for a subset of assets, and callers have no mechanism to detect the failure.

---

### Impact Explanation

Any downstream protocol using `getPricesNoOlderThan` to value a multi-asset portfolio (e.g., a lending protocol computing collateral shortfall, a derivatives protocol computing margin) will receive stale prices for closed-market feeds while believing all prices are fresh. This can cause:

- **Under-collateralization not detected**: stale high price for collateral asset means the protocol thinks collateral is worth more than it is.
- **Over-liquidation**: stale low price for a borrowed asset triggers unnecessary liquidations.
- **Incorrect shortfall/surplus calculations**: directly analogous to the reference bug's `collateralShortfall()` miscalculation.

The impact is financial loss to users or the protocol, matching the Immunefi critical/high scope for price oracle manipulation.

---

### Likelihood Explanation

This is triggered by normal, permissionless keeper operation. Any keeper submitting a valid update for a subscription that mixes crypto and equity (or any two markets with different trading hours) will produce this state. No special access, no malicious intent, and no key compromise is required. The condition is reachable whenever:

1. A subscription contains feeds from markets with different trading hours (a common and documented use case — the code comment at line 362 explicitly anticipates it).
2. A downstream protocol calls `getPricesNoOlderThan` with an `age_seconds` value that is satisfied by the max timestamp but not by the stale feed's individual timestamp.

---

### Recommendation

Replace the single-max-timestamp staleness check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed check. Instead of checking `priceLastUpdatedAt` (the global max), iterate over the requested feeds and verify each individual `price.publishTime` against `age_seconds`:

```solidity
for (uint i = 0; i < priceFeeds.length; i++) {
    if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

Alternatively, store a per-feed `lastUpdatedAt` mapping and check it individually. This ensures the freshness guarantee matches what callers reasonably expect from a function named `getPricesNoOlderThan`.

---

### Proof of Concept

**Setup**: A subscription contains two feeds — `CRYPTO/USD` (crypto, always trading) and `EQUITY/USD` (equity, market closed).

**Step 1**: A keeper calls `updatePriceFeeds` with update data where:
- `CRYPTO/USD.publishTime = block.timestamp - 5` (fresh)
- `EQUITY/USD.publishTime = block.timestamp - 86400` (24 hours old, last trading session)

**Step 2**: `_validateShouldUpdatePrices` computes `updateTimestamp = block.timestamp - 5` (the max). The `TimestampTooOld` check passes (assuming `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` > 5 seconds). The update is accepted. [1](#0-0) 

**Step 3**: `status.priceLastUpdatedAt = block.timestamp - 5` is stored. [2](#0-1) 

**Step 4**: A downstream protocol calls `getPricesNoOlderThan(subscriptionId, [CRYPTO_ID, EQUITY_ID], 60)`.

**Step 5**: `distance(block.timestamp, block.timestamp - 5) = 5 ≤ 60` → check passes. Both prices are returned. [3](#0-2) 

**Step 6**: The caller receives `EQUITY/USD` with `publishTime = block.timestamp - 86400` — 24 hours stale — with no revert and no staleness signal. The caller uses this price in a financial calculation (e.g., collateral valuation), producing an incorrect result.

The same flaw exists in `getEmaPricesNoOlderThan` at lines 578–598, which uses the identical single-max-timestamp guard. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-340)
```text
        // Update status and store the updates
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
