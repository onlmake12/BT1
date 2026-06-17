### Title
`Scheduler.getPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp, Not Per-Feed Timestamps — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getPricesNoOlderThan` and `getEmaPricesNoOlderThan` enforce a staleness check against a single subscription-level timestamp (`priceLastUpdatedAt`), which is set to the **maximum** `publishTime` across all feeds in the last update batch. When a subscription contains a mix of active-market and closed-market assets, the staleness check passes because one fresh feed raises the subscription timestamp, while individual feeds for closed markets may carry timestamps that are hours or days old. The caller receives stale prices without any revert.

---

### Finding Description

In `updatePriceFeeds`, after parsing the Pyth update, `_validateShouldUpdatePrices` computes `updateTimestamp` as the **maximum** `publishTime` across all feeds:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

This value is then stored as the subscription-level `priceLastUpdatedAt`:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
```

The code comment explicitly acknowledges the design intent:

> *"Use the most recent timestamp, as some asset markets may be closed. Closed markets will have a publishTime from their last trading period."*

Later, `getPricesNoOlderThan` checks freshness only against this subscription-level value:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

`getPricesUnsafe` returns the raw stored `price` for every feed in the subscription without any per-feed timestamp check. A subscription containing ETH/USD (active, fresh timestamp) and AAPL/USD (closed market, 16-hour-old timestamp) will pass a `getPricesNoOlderThan(..., 60)` call because `priceLastUpdatedAt` equals ETH's recent timestamp, while AAPL's stale price is silently returned.

The same flaw applies to `getEmaPricesNoOlderThan`, which uses the identical subscription-level check but returns `emaPrice` values.

---

### Impact Explanation

A whitelisted reader (e.g., a DeFi lending or derivatives protocol) calls `getPricesNoOlderThan(subscriptionId, priceIds, 60)` expecting **all** returned prices to be at most 60 seconds old. The function name and NatSpec documentation imply a per-price freshness guarantee. In practice, the function can return prices that are hours or days old for closed-market assets while the call succeeds without reverting. A protocol that uses these prices for collateral valuation, liquidation thresholds, or settlement will operate on stale data, enabling the same class of arbitrage described in the reference report: a user can exploit the divergence between the stale on-chain price and the true market price.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support subscriptions that mix active-market and closed-market assets (the code comment confirms this). Any subscription containing at least one closed-market feed (equities, FX, commodities) alongside an active crypto feed will exhibit this behavior on every update during off-hours. The keeper calling `updatePriceFeeds` is unprivileged and performs a routine operation; no special access or malicious intent is required to trigger the condition. The whitelisted reader is the party harmed.

---

### Recommendation

Replace the subscription-level staleness check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed check against each individual `price.publishTime`:

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

If the intent is to allow closed-market feeds to pass through, the function should be renamed (e.g., `getPricesSubscriptionNoOlderThan`) and its NatSpec must clearly document that the age guarantee applies to the subscription batch timestamp, not to individual feed timestamps. A separate per-feed checked variant should be provided.

---

### Proof of Concept

1. Create a subscription with two price IDs: `ETH_USD` (active market) and `AAPL_USD` (equity, closed market).
2. At 8:00 AM UTC (AAPL market closed since 21:00 UTC prior day), a keeper calls `updatePriceFeeds` with valid Pyth update data. The parsed feeds have:
   - `ETH_USD.price.publishTime = block.timestamp` (fresh)
   - `AAPL_USD.price.publishTime = block.timestamp - 39600` (11 hours old, last close)
3. `_validateShouldUpdatePrices` sets `updateTimestamp = block.timestamp` (ETH's timestamp wins the max). `priceLastUpdatedAt = block.timestamp`.
4. A whitelisted reader calls `getPricesNoOlderThan(subscriptionId, [ETH_USD, AAPL_USD], 60)`.
5. The check `distance(block.timestamp, priceLastUpdatedAt) > 60` evaluates to `0 > 60` → **false** → no revert.
6. `getPricesUnsafe` returns both prices. `AAPL_USD` price is 11 hours old.
7. The reader uses the stale AAPL price for a financial operation, unaware it is not fresh.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
