### Title
Asymmetric Deviation Calculation in `_validateShouldUpdatePrices` Causes Price-Drop Updates to Be Systematically Under-Triggered - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Scheduler contract's deviation check always divides by `previousPrice` (the stored on-chain price). Because the denominator is fixed to the old price regardless of direction, a price drop of a given absolute magnitude produces a smaller `deviationBps` than an equivalent price rise. This means subscriptions configured with `updateOnDeviation` will systematically fail to trigger updates during downward price moves that should cross the threshold, leaving subscribers with stale, inflated prices.

---

### Finding Description

Inside `_validateShouldUpdatePrices`, the deviation is computed as:

```solidity
uint256 numerator = SignedMath.abs(currentPrice - previousPrice);
uint256 denominator = SignedMath.abs(previousPrice);
uint256 deviationBps = Math.mulDiv(numerator, 10_000, denominator);
``` [1](#0-0) 

The denominator is always `previousPrice` — the last stored on-chain value. This is asymmetric:

| Direction | previousPrice | currentPrice | numerator | denominator | deviationBps |
|-----------|--------------|-------------|-----------|-------------|--------------|
| Price rises | 100 | 110 | 10 | 100 | **1000 bps** |
| Price falls | 110 | 100 | 10 | 110 | **909 bps** |

For the exact same absolute price movement (10 units), a rise yields 1000 bps while a fall yields only 909 bps. If `deviationThresholdBps = 1000`, the rise triggers an update but the fall does not — even though the market moved by the same amount.

The check is then:

```solidity
if (deviationBps >= params.updateCriteria.deviationThresholdBps) {
    return updateTimestamp;
}
``` [2](#0-1) 

If no feed crosses the threshold, the function falls through to:

```solidity
revert SchedulerErrors.UpdateConditionsNotMet();
``` [3](#0-2) 

This revert blocks the pusher from storing the new (lower) price, leaving the subscription's stored price stale and elevated.

The `UpdateCriteria` struct shows `deviationThresholdBps` is a `uint32` set by the subscriber at creation time: [4](#0-3) 

---

### Impact Explanation

Subscribers that rely solely on `updateOnDeviation` (or use a long heartbeat) will receive stale, over-valued prices during market downturns. For DeFi protocols (lending, derivatives, liquidations) consuming Scheduler price feeds, this means:

- Collateral is overvalued → bad debt accumulates before liquidations can fire.
- Short positions cannot be settled at the correct lower price.
- Any protocol action gated on a price drop (e.g., liquidation trigger) is delayed or blocked.

The stored price is not updated until either the heartbeat fires or the price recovers enough that the asymmetric formula crosses the threshold — by which time significant damage may have occurred.

---

### Likelihood Explanation

- Any subscription with `updateOnDeviation: true` and a threshold near the expected volatility band is affected.
- The asymmetry is worst near the threshold: a drop of exactly `T%` from a high price will compute as `T * previousHigh / currentLow < T%` bps, failing the check.
- No special attacker capability is needed — the bug manifests purely from normal market price movements. Any unprivileged pusher calling `updatePriceFeeds` will observe the revert.
- Subscriptions without `updateOnHeartbeat` (or with long heartbeats) have no fallback, making the impact persistent.

---

### Recommendation

Replace the fixed `previousPrice` denominator with the **larger** of the two prices, making the deviation symmetric:

```solidity
uint256 absCurrent  = SignedMath.abs(currentPrice);
uint256 absPrevious = SignedMath.abs(previousPrice);
uint256 denominator = absCurrent > absPrevious ? absCurrent : absPrevious;
uint256 deviationBps = Math.mulDiv(
    SignedMath.abs(currentPrice - previousPrice),
    10_000,
    denominator
);
```

Using the larger price as denominator ensures that for any pair `(A, B)`, `getDeviation(A, B) == getDeviation(B, A)`, eliminating the directional asymmetry.

---

### Proof of Concept

**Setup**: Subscription with `updateOnDeviation = true`, `deviationThresholdBps = 1000` (10%), `updateOnHeartbeat = false`.

**Step 1 — Initial update**: pusher calls `updatePriceFeeds` with `price = 110`. Stored successfully (first update, no previous price).

**Step 2 — Price drops to 100**: pusher calls `updatePriceFeeds` with `price = 100`.

Deviation computed:
```
numerator   = |100 - 110| = 10
denominator = |110|        = 110
deviationBps = 10 * 10_000 / 110 = 909 bps
```

`909 < 1000` → `UpdateConditionsNotMet` revert. Stored price remains 110.

**Step 3 — Symmetric scenario (price rises from 100 to 110)**: pusher calls `updatePriceFeeds` with `price = 110`.

Deviation computed:
```
numerator   = |110 - 100| = 10
denominator = |100|        = 100
deviationBps = 10 * 10_000 / 100 = 1000 bps
```

`1000 >= 1000` → update accepted. Stored price becomes 110.

The same absolute price movement (10 units) triggers an update when rising but not when falling, leaving subscribers with a stale elevated price during the downward move. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L424-450)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L453-453)
```text
        revert SchedulerErrors.UpdateConditionsNotMet();
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
