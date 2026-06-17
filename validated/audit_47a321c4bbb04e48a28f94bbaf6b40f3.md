### Title
Scheduler `getPricesNoOlderThan` / `getEmaPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp, Not Per-Feed Timestamp — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` enforce a staleness check against `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the last update batch. For subscriptions that mix always-active feeds (crypto) with market-hours feeds (equities, commodities), the subscription-level timestamp passes the age check while individual closed-market feeds carry `publishTime` values that are hours old. The function name and interface imply a per-feed freshness guarantee that is not actually enforced.

---

### Finding Description

During `updatePriceFeeds()`, `_validateShouldUpdatePrices()` computes `updateTimestamp` as the **maximum** `publishTime` across all feeds in the batch:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

This value is stored as `status.priceLastUpdatedAt`:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
```

Later, `getPricesNoOlderThan()` checks staleness only against this subscription-level value:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

`getPricesUnsafe()` then returns the raw stored `PriceFeed.price` for every requested feed, including those whose individual `publishTime` is far older than `age_seconds`. The staleness check never inspects each feed's own `publishTime`.

The code comment acknowledges the design intent:

> *"Use the most recent timestamp, as some asset markets may be closed. Closed markets will have a publishTime from their last trading period."*

But this design means the function's staleness guarantee is subscription-level, not per-feed — directly contradicting the function's name and the consumer's reasonable expectation.

---

### Impact Explanation

A whitelisted consumer protocol calling `getPricesNoOlderThan(subscriptionId, priceIds, 60)` to ensure all prices are at most 60 seconds old will silently receive stale prices for closed-market feeds (e.g., AAPL/USD with `publishTime` = 6 hours ago) alongside fresh crypto prices. No revert occurs. The consumer has no on-chain signal that individual feeds are stale.

Downstream consequences for protocols using these prices:
- **Lending protocols**: Stale collateral valuations → incorrect liquidation thresholds.
- **Derivatives/perps**: Stale settlement prices → mispriced positions.
- **Any protocol with tight freshness requirements**: Silent staleness defeats the purpose of calling the "no older than" variant.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support mixed subscriptions (crypto + equities). Any subscription containing at least one market-hours feed alongside always-active feeds will exhibit this behavior during off-hours. A keeper/updater submitting a valid update during after-hours triggers the condition. The whitelisted reader then calls `getPricesNoOlderThan()` in good faith and receives stale data without any revert.

---

### Recommendation

Replace the subscription-level staleness check in `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` with a per-feed check:

```solidity
for (uint i = 0; i < prices.length; i++) {
    if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

Alternatively, document clearly that the function only guarantees the subscription's last update cycle was recent, not that every individual feed's `publishTime` satisfies the age bound — and rename the function accordingly (e.g., `getPricesFromRecentUpdateCycle`). Consumers mixing market-hours and always-active feeds should be directed to check individual `publishTime` fields manually.

---

### Proof of Concept

1. Create a Scheduler subscription with two price feeds: `BTC/USD` (always active) and `AAPL/USD` (equity, market hours).
2. During after-hours, a keeper calls `updatePriceFeeds()` with a valid update where:
   - `BTC/USD.publishTime` = `block.timestamp - 5s`
   - `AAPL/USD.publishTime` = `block.timestamp - 21600s` (6 hours, last close)
3. `_validateShouldUpdatePrices()` sets `updateTimestamp = block.timestamp - 5s` (the max). `status.priceLastUpdatedAt = block.timestamp - 5s`.
4. A whitelisted reader calls `getPricesNoOlderThan(subscriptionId, [BTC_ID, AAPL_ID], 60)`.
5. The check `distance(block.timestamp, status.priceLastUpdatedAt) = 5 < 60` passes.
6. `getPricesUnsafe()` returns both feeds. `AAPL/USD` has `publishTime = block.timestamp - 21600s` — 6 hours stale — with no revert.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L361-371)
```text
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

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L68-77)
```text
    /// @notice Returns the price that is no older than `age` seconds of the current time.
    /// @dev This function is a sanity-checked version of `getPriceUnsafe` which is useful in
    /// applications that require a sufficiently-recent price. Reverts if the price wasn't updated sufficiently
    /// recently.
    /// @return prices - please read the documentation of PythStructs.Price to understand how to use this safely.
    function getPricesNoOlderThan(
        uint256 subscriptionId,
        bytes32[] calldata priceIds,
        uint256 age
    ) external view returns (PythStructs.Price[] memory prices);
```
