### Title
Deviation-Only Subscriptions Permanently DoS'd When All Stored Prices Are Zero — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler.sol`, the `_validateShouldUpdatePrices` function silently skips every price feed in the deviation loop when `previousPrice == 0 || currentPrice == 0`. If all feeds in a subscription have a zero stored price (set during the first update), every subsequent deviation check is skipped via `continue`, the loop exits without returning, and the function unconditionally reverts with `UpdateConditionsNotMet`. For subscriptions configured with `updateOnDeviation: true` and `updateOnHeartbeat: false`, this permanently blocks all future updates. For permanent subscriptions (`isPermanent: true`), both `updateSubscription` and `withdrawFunds` are also blocked, locking funds and breaking the subscription irreversibly.

### Finding Description

`_validateShouldUpdatePrices` in `Scheduler.sol` handles the deviation trigger as follows:

```solidity
if (params.updateCriteria.updateOnDeviation) {
    for (uint8 i = 0; i < priceFeeds.length; i++) {
        PythStructs.PriceFeed storage previousFeed = _state
            .priceUpdates[subscriptionId][priceFeeds[i].id];

        if (previousFeed.id == bytes32(0)) {
            return updateTimestamp;   // first-ever update: always passes
        }

        int64 currentPrice  = priceFeeds[i].price.price;
        int64 previousPrice = previousFeed.price.price;

        // Skip if either price is zero to avoid division by zero
        if (previousPrice == 0 || currentPrice == 0) {
            continue;                 // ← silently skips the feed
        }
        // ... deviation math ...
    }
}
revert SchedulerErrors.UpdateConditionsNotMet();   // ← reached when ALL feeds skipped
```

The first update always succeeds because `previousFeed.id == bytes32(0)` triggers an early return. After the first update, `previousFeed.id` is set to the price ID (non-zero), so the early-return guard no longer fires. If the first update stored a price of `0` for every feed, every subsequent call enters the loop, hits `previousPrice == 0` for every feed, `continue`s past all of them, and falls through to the unconditional revert.

The analogous flaw in the external report: when `onchainAccountsTotalBalance == 0`, `fiatPayoutAmount` is forced to `0`, causing the loop to accumulate `accountTokensToBurn` for all fiat accounts even though no on-chain payout exists — the zero-denominator guard silently corrupts the entire distribution. Here, the zero-price guard silently voids the entire deviation check.

### Impact Explanation

1. **Deviation-only subscriptions with zero stored prices are permanently unable to update.** `updatePriceFeeds` always reverts with `UpdateConditionsNotMet`, regardless of how much the price has actually moved.
2. **For permanent subscriptions (`isPermanent: true`):** `updateSubscription` is blocked (cannot add a heartbeat fallback), and `withdrawFunds` is blocked. The subscription's ETH balance is permanently locked in the contract and the subscription is permanently broken.
3. **Stale price data:** Consumers calling `getPricesNoOlderThan` will receive `StalePrice` reverts, breaking any protocol that depends on this subscription.

### Likelihood Explanation

- The Pyth price field is `int64`; a value of `0` is structurally valid. The code itself acknowledges this by adding the zero-skip guard.
- Pyth feeds for newly listed or deprecated assets can legitimately report a price of `0`.
- Any unprivileged keeper can call `updatePriceFeeds` and be the first to store a zero price. The first update always succeeds (the `previousFeed.id == bytes32(0)` guard), so a keeper submitting legitimate Pyth data with a zero price poisons the stored state.
- Subscriptions configured as deviation-only (no heartbeat fallback) are the affected population; this is a documented and supported configuration.

### Recommendation

Replace the `continue` with logic that treats a zero-price feed as an unconditional trigger (or as a "no-data" sentinel that allows the update), mirroring the intent of the `previousFeed.id == bytes32(0)` first-update guard:

```solidity
// If either price is zero, treat as indeterminate — allow the update
if (previousPrice == 0 || currentPrice == 0) {
    return updateTimestamp;
}
```

Alternatively, track whether any feed was actually evaluated; if the loop completes with zero evaluated feeds, treat it as a trigger rather than a rejection.

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription with:
   - `updateOnHeartbeat: false`
   - `updateOnDeviation: true`, `deviationThresholdBps: 100`
   - `isPermanent: true`
2. Submit a first `updatePriceFeeds` call where the Pyth oracle returns `price.price == 0` for all subscribed feeds. This succeeds because `previousFeed.id == bytes32(0)` triggers the early return.
3. Submit a second `updatePriceFeeds` call with any non-zero prices. Observe that `_validateShouldUpdatePrices` enters the deviation loop, hits `previousPrice == 0` for every feed, `continue`s past all of them, and reverts with `UpdateConditionsNotMet`.
4. Attempt `updateSubscription` → reverts `CannotUpdatePermanentSubscription`.
5. Attempt `withdrawFunds` → reverts `CannotUpdatePermanentSubscription`.
6. The subscription's ETH balance is permanently locked and the subscription is permanently broken.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L638-642)
```text

        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L27-32)
```text
    struct UpdateCriteria {
        bool updateOnHeartbeat; // Should update based on time elapsed
        uint32 heartbeatSeconds; // Time interval for heartbeat updates
        bool updateOnDeviation; // Should update based on price deviation
        uint32 deviationThresholdBps; // Price deviation threshold in basis points
    }
```
