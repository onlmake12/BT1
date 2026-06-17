### Title
Permanent DoS of `updatePriceFeeds()` for Deviation-Only Subscriptions When Any Price Feed Is Zero — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, the `_validateShouldUpdatePrices()` function silently skips all price feeds whose `previousPrice` or `currentPrice` is zero to avoid a division-by-zero. When a subscription uses **deviation-only** update criteria (`updateOnHeartbeat = false`, `updateOnDeviation = true`), and all feeds are skipped, the function falls through to an unconditional `revert SchedulerErrors.UpdateConditionsNotMet()`. This permanently blocks `updatePriceFeeds()` for that subscription, causing a DoS of the price update functionality.

---

### Finding Description

In `_validateShouldUpdatePrices()`, the deviation loop contains:

```solidity
// Skip if either price is zero to avoid division by zero
if (previousPrice == 0 || currentPrice == 0) {
    continue;
}
``` [1](#0-0) 

If every feed in the subscription is skipped (because `previousPrice == 0` or `currentPrice == 0`), the loop exits without returning, and execution falls through to:

```solidity
revert SchedulerErrors.UpdateConditionsNotMet();
``` [2](#0-1) 

The heartbeat branch is only entered when `updateOnHeartbeat = true`:

```solidity
if (params.updateCriteria.updateOnHeartbeat) { ... }
if (params.updateCriteria.updateOnDeviation) { ... }
revert SchedulerErrors.UpdateConditionsNotMet();
``` [3](#0-2) 

For a deviation-only subscription, there is no fallback path. The two concrete DoS scenarios are:

1. **Stored zero price**: A previous update stored `price = 0` for a feed. All subsequent updates have `previousPrice == 0`, so the deviation check always skips the feed and always reverts. The subscription is permanently stuck.
2. **Incoming zero price**: The current update reports `price = 0` for all feeds. The deviation check skips all feeds and reverts, even though a price-to-zero transition represents a maximal deviation.

The `UpdateCriteria` struct confirms that `updateOnHeartbeat` and `updateOnDeviation` are independent boolean flags, so a deviation-only subscription is a valid, supported configuration: [4](#0-3) 

Subscription parameter validation enforces that at least one criterion is set, but does not require heartbeat to be enabled: [5](#0-4) 

---

### Impact Explanation

A deviation-only subscription whose stored price data contains a zero value for any feed becomes permanently unserviceable:

- `updatePriceFeeds()` always reverts with `UpdateConditionsNotMet`.
- The subscription's stored price data becomes permanently stale.
- Keepers cannot earn fees for that subscription.
- Consumers reading prices from the subscription receive outdated data indefinitely.

This is a functional DoS of the `updatePriceFeeds()` path for affected subscriptions, directly analogous to the original report's "temporary DoS of swapping functionality."

---

### Likelihood Explanation

Pyth price feeds can legitimately publish a price of `0` for certain assets (e.g., delisted tokens, assets in circuit-breaker states, or misconfigured feeds). A subscription owner who creates a deviation-only subscription and whose feed ever publishes a zero price will trigger this condition. The condition is reachable by any unprivileged keeper calling `updatePriceFeeds()` — no special privileges are required. The likelihood is low-to-medium depending on the asset class, but the impact once triggered is permanent and unrecoverable without a subscription parameter change.

---

### Recommendation

When all feeds are skipped due to zero prices in a deviation-only subscription, the function should treat the update as valid (since a price transitioning to or from zero represents an extreme deviation) rather than reverting. A minimal fix:

```solidity
// Track whether all feeds were skipped due to zero prices
bool allSkipped = true;

for (uint8 i = 0; i < priceFeeds.length; i++) {
    PythStructs.PriceFeed storage previousFeed = _state
        .priceUpdates[subscriptionId][priceFeeds[i].id];

    if (previousFeed.id == bytes32(0)) {
        return updateTimestamp;
    }

    int64 currentPrice = priceFeeds[i].price.price;
    int64 previousPrice = previousFeed.price.price;

    if (previousPrice == 0 || currentPrice == 0) {
        // A transition to/from zero is a maximal deviation — allow update
        if (previousPrice != currentPrice) {
            return updateTimestamp;
        }
        continue;
    }

    allSkipped = false;
    // ... deviation calculation ...
}

if (allSkipped) {
    return updateTimestamp; // All feeds were zero; allow update
}
```

---

### Proof of Concept

1. Deploy `Scheduler` and create a subscription with `updateOnHeartbeat = false`, `updateOnDeviation = true`, `deviationThresholdBps = 100`.
2. Perform a first `updatePriceFeeds()` call where all feeds report `price = 0`. This succeeds (first update, `previousFeed.id == bytes32(0)` triggers early return).
3. Attempt a second `updatePriceFeeds()` call with any non-zero prices. The loop hits `previousPrice == 0` for every feed, `continue`s, and the function reverts with `UpdateConditionsNotMet`.
4. All subsequent calls to `updatePriceFeeds()` for this subscription will permanently revert, regardless of how much the price has changed. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L200-218)
```text
        // Validate update criteria
        if (
            !params.updateCriteria.updateOnHeartbeat &&
            !params.updateCriteria.updateOnDeviation
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
        if (
            params.updateCriteria.updateOnHeartbeat &&
            params.updateCriteria.heartbeatSeconds == 0
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
        if (
            params.updateCriteria.updateOnDeviation &&
            params.updateCriteria.deviationThresholdBps == 0
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L399-454)
```text
        // If updateOnHeartbeat is enabled and the heartbeat interval has passed, trigger update
        if (params.updateCriteria.updateOnHeartbeat) {
            uint256 lastUpdateTime = status.priceLastUpdatedAt;

            if (
                lastUpdateTime == 0 ||
                updateTimestamp >=
                lastUpdateTime + params.updateCriteria.heartbeatSeconds
            ) {
                return updateTimestamp;
            }
        }

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
