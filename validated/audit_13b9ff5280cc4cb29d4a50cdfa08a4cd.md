### Title
`getPricesNoOlderThan()` Staleness Check Uses Subscription-Level Max Timestamp, Allowing Stale Individual Feed Prices to Bypass Freshness Guard — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` functions in the Pyth Pulse `Scheduler` contract perform their staleness check against `status.priceLastUpdatedAt`, which is set to the **maximum** `publishTime` across all price feeds in the subscription. For subscriptions containing both active-market feeds (e.g., crypto) and closed-market feeds (e.g., equities), the staleness check passes based on the most recent feed's timestamp, while individual closed-market feeds may have `publishTime` values far older than the caller-specified `age_seconds`. This violates the function's documented guarantee that all returned prices are no older than the specified age.

### Finding Description

In `getPricesNoOlderThan()`, the staleness check is performed at the subscription level using a single timestamp:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [1](#0-0) 

`status.priceLastUpdatedAt` is set in `updatePriceFeeds()` via `_validateShouldUpdatePrices()` to the **maximum** `publishTime` across all feeds in the update batch:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [2](#0-1) 

This `updateTimestamp` (the max) is then stored as `status.priceLastUpdatedAt`: [3](#0-2) 

The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check in `_validateShouldUpdatePrices()` only enforces that the **maximum** `publishTime` is within the validity window — it does not constrain how old individual feed timestamps can be: [4](#0-3) 

The same flaw exists in `getEmaPricesNoOlderThan()`: [5](#0-4) 

The IScheduler interface documents `getPricesNoOlderThan()` as: *"Returns the price that is no older than `age` seconds of the current time."* This guarantee is violated for any feed in the subscription whose market is closed. [6](#0-5) 

The design comment in the code acknowledges that closed markets will have stale `publishTime` values from their last trading period, but the chosen mitigation (using the max timestamp) breaks the per-feed freshness guarantee: [7](#0-6) 

### Impact Explanation

A downstream protocol calling `getPricesNoOlderThan(subscriptionId, priceIds, age)` trusts that every returned price is no older than `age` seconds. For a subscription containing both crypto feeds (always active) and traditional finance feeds (equities, FX, commodities — closed on weekends/holidays), the staleness check passes based on the crypto feed's recent `publishTime`, while the traditional finance feeds may carry `publishTime` values from days ago. A protocol that uses these prices for financial operations (e.g., collateral valuation, liquidation thresholds, settlement) will operate on stale data it believes to be fresh, enabling an attacker to exploit the price discrepancy.

### Likelihood Explanation

Medium. Pyth Pulse is explicitly designed to serve mixed-asset subscriptions. The vulnerability is triggered passively during every market closure (weekends, holidays) without any attacker action — the stale price is simply the last trading price of the closed market. Any whitelisted reader (or any reader if the whitelist is disabled) can call `getPricesNoOlderThan()` and receive the stale data. The attacker's role is to exploit the downstream protocol that consumes the falsely-certified fresh prices.

### Recommendation

Replace the single subscription-level staleness check in `getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` with a per-feed check against each individual price's `publishTime`:

```solidity
for (uint i = 0; i < prices.length; i++) {
    if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

Alternatively, if the subscription-level check is intentional for closed-market support, update the function name and NatSpec to clearly state that the freshness guarantee applies only to the most recently published feed in the subscription, not to every individual feed.

### Proof of Concept

1. Create a Pulse subscription with two feeds: BTC/USD (crypto, always active) and AAPL/USD (equity, closed on weekends). Heartbeat = 60 seconds.
2. On a Saturday, a keeper calls `updatePriceFeeds()` with update data containing:
   - BTC/USD `publishTime` = `now - 30 minutes` (fresh)
   - AAPL/USD `publishTime` = Friday 4:00 PM (≈ 44 hours ago, last trading close)
3. In `_validateShouldUpdatePrices()`, `updateTimestamp = max(now-30min, Friday 4pm) = now-30min`. This is within `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`, so the update is accepted. `status.priceLastUpdatedAt = now - 30 minutes`.
4. A downstream DeFi protocol calls `getPricesNoOlderThan(subscriptionId, [BTC_ID, AAPL_ID], 3600)` expecting all prices to be at most 1 hour old.
5. `distance(now, now-30min) = 1800 ≤ 3600` → staleness check passes. No revert.
6. The function returns BTC/USD (30 min old — fresh) **and** AAPL/USD (44 hours old — stale), with no indication that AAPL/USD is stale.
7. The downstream protocol uses the stale AAPL/USD price (e.g., Friday's closing price) for collateral valuation or settlement, believing it is no older than 1 hour.
8. An attacker who knows AAPL dropped significantly after Friday's close exploits the inflated stale price in the downstream protocol (e.g., borrows against overvalued AAPL collateral).

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L375-386)
```text
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
