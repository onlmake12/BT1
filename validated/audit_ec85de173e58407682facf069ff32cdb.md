### Title
Single Shared `priceLastUpdatedAt` Across All Feeds Bypasses Per-Feed Staleness Check in `getPricesNoOlderThan` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract stores a single `priceLastUpdatedAt` timestamp in `SubscriptionStatus` that represents the **maximum** publish time across all price feeds in a subscription. The `getPricesNoOlderThan` function uses this single shared value to validate freshness for any individual feed. When a subscription contains feeds with heterogeneous publish times (e.g., a 24/7 crypto feed alongside a market-hours equity feed), the shared maximum timestamp causes the staleness check to pass for feeds whose individual `publishTime` is far older than the requested `age_seconds`.

---

### Finding Description

`SubscriptionStatus.priceLastUpdatedAt` is a single value shared across all price feeds in a subscription: [1](#0-0) 

It is set in `updatePriceFeeds` to the **maximum** publish time across all feeds in the batch: [2](#0-1) [3](#0-2) 

The `getPricesNoOlderThan` function then uses this single shared value to gate freshness for **any** specific feed requested by the caller: [4](#0-3) 

The code itself acknowledges that feeds in the same subscription can have vastly different publish times: [5](#0-4) 

**Concrete scenario:**

A subscription contains two feeds:
- Feed A (BTC/USD, 24/7): `publishTime = block.timestamp - 2 seconds`
- Feed B (AAPL/USD, equity, market closed): `publishTime = block.timestamp - 3 days`

After `updatePriceFeeds`, `priceLastUpdatedAt = block.timestamp - 2 seconds` (the max).

A consumer calls `getPricesNoOlderThan(subscriptionId, [AAPL_ID], 300)`:
1. Staleness check: `distance(block.timestamp, block.timestamp - 2s) = 2 ≤ 300` → **passes**
2. Returns AAPL price with `publishTime = block.timestamp - 3 days` → **3-day-old price returned as "fresh"**

The same flaw exists in `getEmaPricesNoOlderThan`: [6](#0-5) 

---

### Impact Explanation

Any whitelisted reader (or any reader if `whitelistEnabled = false`) calling `getPricesNoOlderThan` or `getEmaPricesNoOlderThan` receives a stale price for a specific feed while the freshness check passes. Protocols that rely on this function's guarantee to protect against stale-price exploits (e.g., lending protocols, AMMs, liquidation engines) will silently consume arbitrarily old prices. This can lead to incorrect liquidations, mispriced collateral, or exploitable arbitrage — a direct financial loss to users of the consuming protocol.

---

### Likelihood Explanation

The Scheduler is explicitly designed to support mixed-asset subscriptions (crypto + equities/FX). The code comment at line 362–365 acknowledges that "closed markets will have a publishTime from their last trading period." Any subscription combining a 24/7 crypto feed with a market-hours feed will trigger this condition during off-hours. An unprivileged reader (no special role required) can call `getPricesNoOlderThan` at any time. No attacker action is needed — the condition arises naturally from normal protocol operation.

---

### Recommendation

Track `priceLastUpdatedAt` per price feed ID rather than as a single value per subscription. Change `SubscriptionStatus` to use a mapping:

```solidity
// Instead of:
uint256 priceLastUpdatedAt;

// Use:
mapping(bytes32 => uint256) priceLastUpdatedAt; // priceId => last publish time
```

In `getPricesNoOlderThan`, check each requested feed's individual timestamp against `age_seconds` rather than the subscription-wide maximum. In `updatePriceFeeds`, store each feed's own `publishTime` into this per-feed mapping.

---

### Proof of Concept

```solidity
// 1. Create subscription with BTC/USD (crypto) + AAPL/USD (equity)
uint256 subId = scheduler.createSubscription{value: minBalance}(params);

// 2. Keeper updates: BTC publishTime = now, AAPL publishTime = now - 3 days
//    priceLastUpdatedAt is set to max(now, now-3days) = now
scheduler.updatePriceFeeds(subId, updateData);

// 3. Reader requests AAPL with 5-minute freshness guarantee
// Staleness check: distance(block.timestamp, now) = 0 <= 300 → PASSES
// But returned AAPL price has publishTime = now - 3 days
PythStructs.Price[] memory prices = scheduler.getPricesNoOlderThan(
    subId,
    [AAPL_PRICE_ID],
    300  // 5 minutes
);
// prices[0].publishTime == block.timestamp - 3 days  ← stale price returned as fresh
``` [7](#0-6)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L19-24)
```text
    struct SubscriptionStatus {
        uint256 priceLastUpdatedAt; // Timestamp of the last update. All feeds in the subscription are updated together.
        uint256 balanceInWei; // Balance that will be used to fund the subscription's upkeep.
        uint256 totalUpdates; // Tracks update count across all feeds in the subscription (increments by number of feeds per update)
        uint256 totalSpent; // Counter of total fees paid for subscription upkeep in wei.
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L356-397)
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

        // Calculate the minimum acceptable timestamp (clamped at 0)
        // The maximum acceptable timestamp is enforced by the parsePriceFeedUpdatesWithSlots call
        uint256 minAllowedTimestamp = (block.timestamp >
            PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            : 0;

        // Validate that the update timestamp is not too old
        if (updateTimestamp < minAllowedTimestamp) {
            revert SchedulerErrors.TimestampTooOld(
                updateTimestamp,
                block.timestamp
            );
        }

        // Reject updates if they're older than the latest stored ones
        if (
            status.priceLastUpdatedAt > 0 &&
            updateTimestamp <= status.priceLastUpdatedAt
        ) {
            revert SchedulerErrors.TimestampOlderThanLastUpdate(
                updateTimestamp,
                status.priceLastUpdatedAt
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
