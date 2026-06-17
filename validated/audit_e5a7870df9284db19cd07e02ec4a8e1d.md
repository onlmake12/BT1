### Title
`Scheduler.getPricesNoOlderThan()` Uses Subscription-Level Max Timestamp Instead of Per-Feed Timestamps, Allowing Stale Individual Prices to Pass Freshness Checks - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getPricesNoOlderThan()` validates freshness using a single subscription-level timestamp (`status.priceLastUpdatedAt`), which is set to the **maximum** `publishTime` across all feeds in the batch. Individual feeds — particularly closed-market assets — can carry timestamps days older than the requested age, yet the staleness check passes because it only compares against the subscription-wide maximum. Consumers relying on this function for per-feed freshness guarantees silently receive stale prices.

---

### Finding Description

In `_validateShouldUpdatePrices()`, the `updateTimestamp` is computed as the maximum `publishTime` across all price feeds in the submitted batch:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [1](#0-0) 

This maximum value is then stored as the subscription's last-updated timestamp:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
``` [2](#0-1) 

When a consumer calls `getPricesNoOlderThan()`, the staleness check compares `block.timestamp` against this subscription-level max, **not** against individual feed `publishTime` values:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) 

The code explicitly allows closed-market feeds to carry old timestamps by design:

```
// Note: We don't want to reject update data if it contains a price
// from a market that closed a few days ago, since it will contain a timestamp
// from the last trading period.
``` [4](#0-3) 

However, `getPricesNoOlderThan()` does not communicate this caveat to callers. The function's name and NatSpec promise that all returned prices are no older than `age_seconds`, but the implementation only guarantees that **at least one feed** in the subscription satisfies that bound.

The `updatePriceFeeds()` entry point is fully permissionless — any unprivileged keeper can call it: [5](#0-4) 

The `IScheduler` interface documents `getPricesNoOlderThan()` as a "sanity-checked version of `getPriceUnsafe`" that "reverts if the price wasn't updated sufficiently recently," reinforcing the false guarantee: [6](#0-5) 

---

### Impact Explanation

A downstream DeFi protocol (lending, derivatives, etc.) that subscribes to a mixed feed set (e.g., BTC/USD + an equity index) and calls `getPricesNoOlderThan(subscriptionId, priceIds, 60)` expecting all prices to be fresh within 60 seconds will silently receive a price that is days old for the closed-market feed. Any financial operation — collateral valuation, liquidation threshold, settlement — that depends on that price will use stale data without any on-chain revert or warning. This is the direct analog of H-04: prices are passed through to consumers without per-feed outlier/staleness validation.

---

### Likelihood Explanation

The scenario is realistic and low-effort:
1. Subscriptions mixing active crypto feeds with equity/commodity feeds are a natural use case for Pulse.
2. Any unprivileged keeper can submit a valid Pyth update containing a mix of fresh and closed-market feeds.
3. The Scheduler's own design explicitly permits this (the `minAllowedPublishTime = 0` comment confirms it).
4. Consumers following the README's recommendation to use `getPricesNoOlderThan()` are directly exposed.

---

### Recommendation

Replace the subscription-level staleness check in `getPricesNoOlderThan()` with a per-feed check against each individual `price.publishTime`:

```solidity
for (uint i = 0; i < priceFeeds.length; i++) {
    if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

Alternatively, document clearly that `getPricesNoOlderThan()` only guarantees the freshness of the most recently updated feed in the subscription, and rename or add a separate `getAllPricesNoOlderThan()` variant that enforces per-feed freshness.

---

### Proof of Concept

1. Manager creates a subscription with two feeds: `BTC_USD` (active, 24/7) and `SPY_USD` (equity, closed on weekends).
2. On a Monday morning, a keeper submits a valid Pyth update where `BTC_USD.publishTime = now` and `SPY_USD.publishTime = Friday_close` (≈ 60 hours ago).
3. `_validateShouldUpdatePrices()` sets `updateTimestamp = now` (max of the two).
4. `status.priceLastUpdatedAt = now`.
5. A lending protocol calls `getPricesNoOlderThan(subscriptionId, [SPY_USD], 60)`.
6. `distance(now, now) = 0 < 60` → **no revert**.
7. The protocol receives Friday's closing price for SPY, 60 hours stale, and uses it for collateral valuation — identical in structure to the H-04 pattern where `pricePerShare` is used without outlier/freshness validation per asset.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-278)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L300-304)
```text
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
