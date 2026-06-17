### Title
Deviation-Only Subscriptions Permanently Blocked When All Stored Prices Are Zero — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
In `_validateShouldUpdatePrices`, when a subscription is configured with `updateOnDeviation = true` and `updateOnHeartbeat = false`, if all stored previous prices are zero, the deviation-check loop silently `continue`s over every feed and falls through to `revert SchedulerErrors.UpdateConditionsNotMet()`. This permanently prevents any further deviation-triggered update for that subscription, creating a liveness DoS.

### Finding Description
`_validateShouldUpdatePrices` evaluates two independent trigger criteria in sequence:

1. **Heartbeat** — if enabled and the interval has elapsed, returns immediately.
2. **Deviation** — if enabled, iterates over all price feeds and returns if any feed's deviation exceeds the threshold.

Inside the deviation loop there is an explicit zero-price guard:

```solidity
// Skip if either price is zero to avoid division by zero
if (previousPrice == 0 || currentPrice == 0) {
    continue;          // ← silently skips the feed
}
```

When **every** feed in the subscription satisfies this guard (i.e., every stored `previousPrice` or every incoming `currentPrice` is zero), the loop body never executes a `return`. Control falls through to:

```solidity
revert SchedulerErrors.UpdateConditionsNotMet();
```

This is the same error that fires when no deviation threshold is crossed, so callers receive no indication that the real cause is zero prices. Because the zero prices were written to storage by the first successful update, every subsequent call to `updatePriceFeeds` for that subscription will hit the same path and revert — permanently blocking deviation-triggered updates.

The first update always succeeds because the guard `if (previousFeed.id == bytes32(0)) { return updateTimestamp; }` fires before the zero-price check. It is only after the first update stores zero prices that the subscription becomes stuck. [1](#0-0) 

### Impact Explanation
A subscription configured with `updateOnDeviation = true` and `updateOnHeartbeat = false` whose stored prices are all zero can never be updated through the normal keeper path. Price data for that subscription becomes permanently stale. Any protocol reading prices from the Scheduler for that subscription will consume outdated values. The keeper also loses the ability to earn fees for that subscription.

### Likelihood Explanation
Pyth prices are `int64` and can legitimately be zero (e.g., during a data outage, a market halt, or for synthetic/test feeds). The scenario requires:
1. A subscription with deviation-only criteria.
2. A first update that stores zero prices (possible when Pyth returns zero for all subscribed feeds).

Neither step requires a privileged role; any user can create such a subscription and any keeper can submit the first update. The likelihood is low for production assets but non-negligible for edge-case feeds or during data-quality incidents.

### Recommendation
When the deviation loop completes without finding a qualifying feed because **all** feeds were skipped due to zero prices, the function should not silently fall through to `UpdateConditionsNotMet`. Two safe options:

**Option A** — treat a fully-skipped loop as a valid update (conservative, matches the intent of "no deviation can be computed, so allow the update"):
```solidity
bool anyChecked = false;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (previousPrice == 0 || currentPrice == 0) continue;
    anyChecked = true;
    if (deviationBps >= params.updateCriteria.deviationThresholdBps)
        return updateTimestamp;
}
if (!anyChecked) return updateTimestamp; // all prices were zero
```

**Option B** — revert with a dedicated error so callers can distinguish the cause:
```solidity
revert SchedulerErrors.AllPricesZeroOrUndefined();
```

### Proof of Concept
1. Deploy `Scheduler` and create a subscription with `updateOnDeviation = true`, `updateOnHeartbeat = false`, one price feed.
2. Mock Pyth to return `price = 0` for that feed. Call `updatePriceFeeds` — succeeds (first update, `previousFeed.id == bytes32(0)` path). Zero price is now stored.
3. Mock Pyth to return any non-zero price for the same feed. Call `updatePriceFeeds` again.
4. Observe: `previousPrice == 0` → `continue` → loop ends → `revert UpdateConditionsNotMet()`.
5. The subscription can never be updated via the deviation trigger again. [2](#0-1) [3](#0-2)

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
