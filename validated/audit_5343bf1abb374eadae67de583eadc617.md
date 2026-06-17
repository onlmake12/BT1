### Title
Deviation Check Silently Skips Zero-Price Feeds, Blocking Legitimate Updates - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler._validateShouldUpdatePrices`, when a price feed reports a current price of zero, the deviation check is silently skipped via `continue`. For subscriptions configured with `updateOnDeviation: true` only (no heartbeat), this causes the function to revert with `UpdateConditionsNotMet` even when a price has dropped to zero — the most extreme possible deviation.

### Finding Description

In `_validateShouldUpdatePrices` (lines 412–453), the deviation-based update trigger iterates over all price feeds. When either `previousPrice` or `currentPrice` is zero, the code skips the feed entirely:

```solidity
// Skip if either price is zero to avoid division by zero
if (previousPrice == 0 || currentPrice == 0) {
    continue;
}
```

The comment justifies this as avoiding division by zero in the `Math.mulDiv` call below. However, the consequence is that a feed whose price has dropped to zero — representing an infinite (100%) deviation — is treated as if no deviation occurred. The loop moves on to the next feed, and if no other feed meets the threshold, the function falls through to:

```solidity
revert SchedulerErrors.UpdateConditionsNotMet();
```

This is structurally identical to the reported vulnerability: a guard condition uses the wrong control flow (`continue`/silent skip instead of triggering the update), causing the function to produce an incorrect outcome when a zero value is encountered.

The correct behavior when `currentPrice == 0` and `previousPrice != 0` is to treat this as a maximum-deviation event and return `updateTimestamp` immediately, since the price has moved by 100%.

### Impact Explanation

For any subscription configured with `updateOnDeviation: true` and `updateOnHeartbeat: false`:

1. A keeper calls `updatePriceFeeds()` with valid Pyth price data where one or more feeds report a price of zero.
2. `_validateShouldUpdatePrices` skips those feeds' deviation checks.
3. If no remaining feed meets the `deviationThresholdBps`, the function reverts with `UpdateConditionsNotMet`.
4. The Scheduler's stored price for that feed is never updated to zero.
5. Consumers calling `getPricesUnsafe` or `getPricesNoOlderThan` on the Scheduler receive the stale non-zero price.

This causes incorrect price data to persist in the Scheduler, which downstream protocols consuming Scheduler prices may use for financial decisions (e.g., liquidations, collateral valuation).

**Impact: Medium** — stale/incorrect prices in the Scheduler for deviation-only subscriptions when a feed price drops to zero.

### Likelihood Explanation

**Likelihood: Medium** — Zero prices can occur for:
- Assets during market closure periods (some Pyth feeds report zero for closed markets)
- Data feed anomalies or publisher outages
- Newly listed assets with sparse publisher coverage

Subscriptions configured with deviation-only criteria (no heartbeat) are a supported and documented configuration. The combination is realistic.

### Recommendation

Replace the `continue` with a return that triggers the update when `currentPrice == 0` (a 100% deviation from any non-zero previous price):

```solidity
if (previousPrice == 0) {
    // Cannot compute deviation; skip this feed
    continue;
}
if (currentPrice == 0) {
    // Price dropped to zero: treat as maximum deviation, trigger update
    return updateTimestamp;
}
```

This preserves the division-by-zero guard for `previousPrice == 0` while correctly handling the case where the current price is zero.

### Proof of Concept

1. Deploy `Scheduler` and create a subscription with:
   - `updateOnHeartbeat: false`
   - `updateOnDeviation: true`, `deviationThresholdBps: 100` (1%)
   - One price feed, e.g., feed ID `0xABC`
2. Call `updatePriceFeeds` with `price = 1000` to set the initial stored price.
3. Call `updatePriceFeeds` again with `price = 0` for the same feed (a valid Pyth update).
4. Observe: the call reverts with `UpdateConditionsNotMet` even though the price moved from 1000 to 0 (100% deviation, far exceeding the 1% threshold).
5. The Scheduler continues to serve `price = 1000` to consumers via `getPricesUnsafe`.

Root cause confirmed at: [1](#0-0) 

The deviation-only subscription path that falls through to revert: [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L412-453)
```text
        // If updateOnDeviation is enabled, check if any price has deviated enough
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
```
