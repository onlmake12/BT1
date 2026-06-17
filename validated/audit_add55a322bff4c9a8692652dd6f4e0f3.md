### Title
Staleness Check in `getPricesNoOlderThan` Uses Max Subscription Timestamp Instead of Per-Price Timestamp — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `getPricesNoOlderThan` (and `getEmaPricesNoOlderThan`) validates freshness using `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the subscription. However, individual price feeds returned may have `publishTime` values far older than this maximum. This creates the same class of inconsistency as the BunniToken report: the validation check uses a more favorable (broader) metric than what the returned data actually represents.

---

### Finding Description

In `_validateShouldUpdatePrices`, the subscription's `updateTimestamp` is computed as the **maximum** `publishTime` across all price feeds:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [1](#0-0) 

This `updateTimestamp` is then stored as `status.priceLastUpdatedAt`:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
``` [2](#0-1) 

`getPricesNoOlderThan` then uses this single max-timestamp value for its staleness check:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) 

`getEmaPricesNoOlderThan` has the identical pattern: [4](#0-3) 

The mismatch: the staleness check passes if **any one feed** in the subscription has a recent timestamp, but the function then returns **all requested feeds**, including those whose individual `publishTime` may be hours or days old (e.g., closed-market feeds like equities or commodities).

The code comment acknowledges this design explicitly:

> "Use the most recent timestamp, as some asset markets may be closed. Closed markets will have a publishTime from their last trading period." [5](#0-4) 

This is the inconsistency: the comment justifies using the max timestamp for the **update trigger** logic, but the same max timestamp is then reused as the staleness gate in `getPricesNoOlderThan`, which consumers rely on for per-price freshness guarantees.

---

### Impact Explanation

A protocol consuming prices via `getPricesNoOlderThan(subscriptionId, [equityFeedId], 60)` expects every returned price to be no older than 60 seconds. Instead, it may silently receive a price for a closed equity market that was last published 8+ hours ago, because the staleness check passes based on a co-subscribed crypto feed's recent timestamp. This can lead to incorrect valuations, bad liquidations, or mispriced derivatives in any protocol that relies on the freshness guarantee implied by the function name.

---

### Likelihood Explanation

Any subscription that mixes feeds with different market hours (e.g., BTC/USD alongside AAPL/USD) will exhibit this behavior by design. A keeper calling `updatePriceFeeds` with valid Pyth-signed data is an unprivileged, externally reachable entry point. No special access is required. The scenario is realistic for any multi-asset protocol using the Scheduler.

---

### Recommendation

Replace the single subscription-level `priceLastUpdatedAt` staleness check in `getPricesNoOlderThan` with a per-price-feed staleness check. Iterate over the requested price feeds and validate that each individual feed's `publishTime` satisfies the `age_seconds` constraint before returning it:

```solidity
for (uint i = 0; i < priceFeeds.length; i++) {
    if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

Apply the same fix to `getEmaPricesNoOlderThan`. This aligns the validation metric with the data actually returned, matching the semantics implied by the function name.

---

### Proof of Concept

1. A subscription is created with two feeds: `BTC/USD` (active, 24/7) and `AAPL/USD` (equity, closed overnight).
2. A keeper calls `updatePriceFeeds` at 10 PM with valid Pyth data. `BTC/USD` has `publishTime = now`, `AAPL/USD` has `publishTime = now - 8 hours` (last close).
3. `_validateShouldUpdatePrices` computes `updateTimestamp = max(now, now-8h) = now`. The update passes. `status.priceLastUpdatedAt = now`.
4. At 10:00:30 PM, a consumer calls `getPricesNoOlderThan(subscriptionId, [AAPL_id], 60)`.
5. The staleness check: `distance(block.timestamp, priceLastUpdatedAt) = 30s ≤ 60s` → **passes**.
6. The function returns the AAPL price with `publishTime = now - 8 hours`, which is 8 hours stale — directly contradicting the `NoOlderThan(60)` guarantee. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L340-340)
```text
        status.priceLastUpdatedAt = latestPublishTime;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L356-371)
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
