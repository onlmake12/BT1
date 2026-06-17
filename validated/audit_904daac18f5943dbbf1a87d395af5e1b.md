### Title
Deviation Check Silently Skips Zero-Priced Feeds, Blocking Valid Updates - (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler::_validateShouldUpdatePrices()`, when `previousPrice == 0 || currentPrice == 0`, the deviation check is silently skipped via `continue`. A price of 0 represents an infinite deviation from any non-zero price and should unconditionally trigger an update. When all feeds in a subscription have a zero current or previous price, the entire deviation loop completes without returning, and the function falls through to `revert UpdateConditionsNotMet`, permanently blocking the update.

### Finding Description

`_validateShouldUpdatePrices` in `Scheduler.sol` iterates over all price feeds to determine whether any has deviated beyond the configured threshold:

```solidity
// Skip if either price is zero to avoid division by zero
if (previousPrice == 0 || currentPrice == 0) {
    continue;
}
``` [1](#0-0) 

The intent is to avoid a division-by-zero in the `Math.mulDiv` call that follows. However, `continue` silently discards the feed from the deviation decision entirely. A price of 0 is not a neutral sentinel — it represents the maximum possible deviation (infinite) from any non-zero price. The correct behavior is to treat it as an unconditional trigger, not a skip.

When **all** feeds in a subscription have `previousPrice == 0` (stored from a prior update) or `currentPrice == 0` (in the incoming update), the loop exits without ever calling `return updateTimestamp`, and the function reaches:

```solidity
revert SchedulerErrors.UpdateConditionsNotMet();
``` [2](#0-1) 

This blocks the update entirely, even though prices changed from 0 to non-zero (or vice versa) — the largest possible deviation.

The `updatePriceFeeds` entry point is permissionless (any keeper/relayer can call it), and the prices are sourced from `parsePriceFeedUpdatesWithConfig` with `minAllowedPublishTime = 0`: [3](#0-2) 

### Impact Explanation

A subscription configured with `updateOnDeviation = true` and `updateOnHeartbeat = false` will fail to update when prices transition to or from 0. The subscription's stored prices remain stale. Any consumer reading prices from the subscription via `getPricesUnsafe` or `getPricesNoOlderThan` will receive incorrect data. For DeFi protocols relying on the subscription's price freshness (e.g., for liquidations, collateral valuation, or AMM pricing), this stale data can cause direct financial loss.

### Likelihood Explanation

Pyth price feeds can legitimately report `price = 0` for halted markets, newly listed assets, or assets whose value collapses to zero. A keeper submitting such a valid Pyth update stores `price = 0` in the subscription. When prices subsequently recover, the deviation trigger is permanently silenced for all feeds that previously stored 0. The entry path is fully unprivileged — any address can call `updatePriceFeeds`. [4](#0-3) 

### Recommendation

Replace `continue` with an unconditional trigger when either price is zero, since a zero price represents infinite deviation:

```solidity
// A zero price represents infinite deviation — always trigger
if (previousPrice == 0 || currentPrice == 0) {
    return updateTimestamp;
}
```

### Proof of Concept

1. Create a subscription with `updateOnDeviation = true`, `updateOnHeartbeat = false`, `deviationThresholdBps = 100`.
2. Submit a first update where all feeds have `price = 0`. This is accepted (first update, `previousFeed.id == bytes32(0)` triggers early return). Stored prices are now 0.
3. Submit a second update where all feeds have `price = 1_000_000` (massive deviation from 0).
4. In `_validateShouldUpdatePrices`, for each feed: `previousPrice = 0`, so `continue` is hit.
5. The loop exits without returning. The function reverts with `UpdateConditionsNotMet`.
6. The subscription's stored prices remain at 0 despite the real price being 1,000,000.
7. Consumers of the subscription read `price = 0` indefinitely (until a heartbeat-based update fires, if one is configured). [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-278)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L311-319)
```text
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L413-454)
```text
        if (params.updateCriteria.updateOnDeviation) {
            for (uint8 i = 0; i < priceFeeds.length; i++) {
                // Get the previous price feed for this price ID using subscriptionId
                PythStructs.PriceFeed storage previousFeed = _state
                    .priceUpdates[subscriptionId][priceFeeds[i].id];

                // If there's no previous price, this is the first update
                if (previousFeed.id == bytes32(0)) {
                    return updateTimestamp;
                }

                // Calculate the deviation percentage
                int64 currentPrice = priceFeeds[i].price.price;
                int64 previousPrice = previousFeed.price.price;

                // Skip if either price is zero to avoid division by zero
                if (previousPrice == 0 || currentPrice == 0) {
                    continue;
                }

                // Calculate absolute deviation basis points (scaled by 1e4)
                uint256 numerator = SignedMath.abs(
                    currentPrice - previousPrice
                );
                uint256 denominator = SignedMath.abs(previousPrice);
                uint256 deviationBps = Math.mulDiv(
                    numerator,
                    10_000,
                    denominator
                );

                // If deviation exceeds threshold, trigger update
                if (
                    deviationBps >= params.updateCriteria.deviationThresholdBps
                ) {
                    return updateTimestamp;
                }
            }
        }

        revert SchedulerErrors.UpdateConditionsNotMet();
    }
```
