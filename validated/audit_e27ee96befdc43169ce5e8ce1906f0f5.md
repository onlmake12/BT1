### Title
Scheduler's `getPricesNoOlderThan` Uses Subscription-Level Timestamp Instead of Per-Feed Timestamps, Allowing Stale Prices to Pass Freshness Checks - (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` validate freshness using a single subscription-level timestamp (`priceLastUpdatedAt`) rather than each individual feed's `publishTime`. A subscription containing mixed-frequency feeds (e.g., crypto + equities) will have `priceLastUpdatedAt` driven by the most recently updated feed, while slow-updating or closed-market feeds retain stale stored prices. Callers relying on `getPricesNoOlderThan` to guarantee freshness will silently receive stale prices for individual feeds.

---

### Finding Description

`updatePriceFeeds` in `Scheduler.sol` computes `updateTimestamp` as the **maximum** `publishTime` across all feeds in the submitted update batch: [1](#0-0) 

This maximum is then stored as the subscription's `priceLastUpdatedAt`: [2](#0-1) 

The code explicitly acknowledges that closed-market feeds will carry old timestamps from their last trading period, and deliberately accepts them by using `minAllowedPublishTime = 0`: [3](#0-2) 

Each feed's stored price is written with its own (potentially old) `publishTime`: [4](#0-3) 

However, `getPricesNoOlderThan` only checks the subscription-level `priceLastUpdatedAt` against `age_seconds`, not each individual feed's `publishTime`: [5](#0-4) 

`getEmaPricesNoOlderThan` has the identical flaw: [6](#0-5) 

**Concrete scenario:**
1. Subscription contains BTC/USD (updates every second) and AAPL/USD (US equity, market closed).
2. Keeper submits update: BTC/USD `publishTime = now - 5s`, AAPL/USD `publishTime = 8 hours ago`.
3. `updateTimestamp = now - 5s` (max), passes `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check.
4. `priceLastUpdatedAt = now - 5s`.
5. Reader calls `getPricesNoOlderThan(subscriptionId, [BTC_ID, AAPL_ID], 60)`.
6. Check: `distance(now, now-5) = 5 > 60`? → **No, passes.**
7. Returns AAPL price with an 8-hour-old `publishTime` as if it were fresh.

---

### Impact Explanation

Protocols (e.g., lending/borrowing, liquidation engines) that integrate the Scheduler and call `getPricesNoOlderThan` to guarantee price freshness will silently receive stale prices for slow-updating or closed-market feeds. This can lead to:

- **Incorrect collateral valuation** using stale prices, enabling over-borrowing.
- **Missed or unfair liquidations** because stale collateral prices are accepted as current.
- **Incorrect mark-to-market** for derivative protocols.

The impact directly mirrors the reported external vulnerability: a single staleness threshold applied at the wrong granularity causes stale prices to be reported as valid.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support mixed-frequency subscriptions (the comment at line 299–304 confirms this). Any subscription combining crypto feeds with equity, FX, or commodity feeds will exhibit this behavior during non-trading hours. This is a common and expected deployment pattern, making the likelihood high for real-world integrators.

---

### Recommendation

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` should validate each individual feed's `publishTime` against `age_seconds`, not the subscription-level `priceLastUpdatedAt`. For example:

```solidity
function getPricesNoOlderThan(
    uint256 subscriptionId,
    bytes32[] calldata priceIds,
    uint256 age_seconds
) external view override onlyWhitelistedReader(subscriptionId) returns (PythStructs.Price[] memory prices) {
    prices = _getPricesInternal(subscriptionId, priceIds);
    for (uint i = 0; i < prices.length; i++) {
        if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
            revert PythErrors.StalePrice();
    }
}
```

Alternatively, document clearly that `getPricesNoOlderThan` does **not** guarantee per-feed freshness and that callers must check individual `publishTime` fields themselves.

---

### Proof of Concept

1. Deploy a Scheduler subscription with two price IDs: `BTC_USD` and `AAPL_USD`.
2. Submit an `updatePriceFeeds` call where `BTC_USD.publishTime = block.timestamp - 5` and `AAPL_USD.publishTime = block.timestamp - 28800` (8 hours ago, simulating a closed market).
3. Observe that `priceLastUpdatedAt` is set to `block.timestamp - 5`.
4. Call `getPricesNoOlderThan(subscriptionId, [BTC_USD, AAPL_USD], 60)`.
5. The call succeeds and returns AAPL's price with `publishTime = block.timestamp - 28800`, despite the caller requesting prices no older than 60 seconds. [5](#0-4) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L299-318)
```text
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
