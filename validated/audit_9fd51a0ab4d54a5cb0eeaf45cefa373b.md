### Title
Adversarial Price Selection via 1-Hour Timestamp Window in `Scheduler.updatePriceFeeds()` Enables Stale Price Injection — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler.updatePriceFeeds()` function accepts price updates whose `publishTime` falls anywhere within the past 1 hour (`PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours`). Because `updatePriceFeeds()` is callable by any unprivileged keeper, an attacker can adversarially cherry-pick a price from within that 1-hour window — selecting whichever historical price is most favorable — and inject it as the "current" price stored in the Scheduler. This is the direct analog of the external report's time-window price selection bug.

---

### Finding Description

In `Scheduler.sol`, `updatePriceFeeds()` calls `parsePriceFeedUpdatesWithConfig` with `minPublishTime = 0` and `maxPublishTime = block.timestamp + 10 seconds`: [1](#0-0) 

The past-side bound is then enforced separately in `_validateShouldUpdatePrices`: [2](#0-1) 

The constant is: [3](#0-2) 

The effective accepted window is therefore **`[block.timestamp − 3600s, block.timestamp + 10s]`** — a full hour of selectable prices.

The `updateTimestamp` used for all downstream logic is derived as the **maximum** `publishTime` across all submitted feeds: [4](#0-3) 

This value is then stored as `priceLastUpdatedAt`: [5](#0-4) 

Because `updatePriceFeeds()` is permissionless (no `onlyOwner` or similar guard), any address can call it: [6](#0-5) 

The Pyth Hermes API exposes historical price data at arbitrary past timestamps, so a keeper can trivially fetch any price from within the past hour and submit it.

**Attack path for deviation-based subscriptions:**

1. Subscription is configured with `updateOnDeviation = true`, e.g. `deviationThresholdBps = 100` (1%).
2. The stored price is $100. The current live price is $100.40 (0.4% deviation — below threshold, no legitimate update would fire).
3. Attacker fetches a Pyth price update from 45 minutes ago when the price was $102 (2% deviation).
4. Attacker calls `updatePriceFeeds()` with that 45-minute-old update data. The `publishTime` is `block.timestamp − 2700`, which satisfies `>= block.timestamp − 3600`.
5. `_validateShouldUpdatePrices` computes `updateTimestamp = block.timestamp − 2700`, passes the staleness check, and the deviation check fires because `|102 − 100| / 100 = 2% >= 1%`.
6. The Scheduler stores the stale $102 price and sets `priceLastUpdatedAt = block.timestamp − 2700`.
7. Consumers calling `getPricesNoOlderThan(subscriptionId, priceIds, 3600)` receive $102 — a price that is 45 minutes old and 1.6% away from the true current price. [7](#0-6) 

The staleness check in `getPricesNoOlderThan` uses `distance(block.timestamp, status.priceLastUpdatedAt)`, which equals 2700 seconds — well within the consumer's 3600-second age limit — so no revert occurs.

---

### Impact Explanation

Consumers of the Scheduler (financial protocols using it for liquidations, settlements, or collateral valuation) receive a stale, adversarially selected price. For deviation-triggered subscriptions the attacker can force an update at any time by replaying a historical price that exceeds the deviation threshold, even when the live price has not actually moved enough to warrant an update. For heartbeat-triggered subscriptions the attacker can supply a price from up to 59 minutes in the past immediately after the heartbeat interval elapses, ensuring consumers see a significantly outdated price. Both cases can cause incorrect financial outcomes for protocols that rely on the Scheduler.

---

### Likelihood Explanation

The entry point is fully permissionless — any EOA or contract can call `updatePriceFeeds()`. The Pyth Hermes historical API (`/v1/updates/price/{timestamp}`) makes it trivial to retrieve a signed price update for any past timestamp. No privileged access, leaked key, or governance majority is required. The attacker only needs to pay the Pyth update fee (typically a few wei) and the gas cost of the transaction.

---

### Recommendation

1. **Tighten the past validity window.** Reduce `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` from 1 hour to a value appropriate for live-market feeds (e.g., 60–120 seconds). Closed-market feeds can be handled by a separate code path or by allowing the subscription owner to configure a per-subscription staleness tolerance.
2. **Use `parsePriceFeedUpdatesUnique`** (or set `checkUniqueness = true`) with a non-zero `minPublishTime` equal to `priceLastUpdatedAt`. This forces the submitted price to be the *first* Pythnet update after the last stored timestamp, eliminating the ability to cherry-pick a favorable historical price.
3. **Validate `publishTime` against `block.timestamp` directly** in `_validateShouldUpdatePrices`, not just against a wide window, so that the accepted price is provably recent.

---

### Proof of Concept

```solidity
// Attacker contract
contract SchedulerPriceManipulator {
    IScheduler scheduler;
    IHermes hermes; // off-chain: fetch via Hermes REST API

    // subscriptionId has updateOnDeviation=true, deviationThresholdBps=100
    // stored price: $100, current live price: $100.40 (below 1% threshold)
    function exploit(uint256 subscriptionId) external payable {
        // 1. Off-chain: fetch Hermes update for timestamp = block.timestamp - 2700
        //    where price was $102 (2% above stored $100)
        bytes[] memory updateData = fetchHistoricalUpdate(block.timestamp - 2700);

        // 2. Call updatePriceFeeds — no access control, anyone can call
        scheduler.updatePriceFeeds(subscriptionId, updateData);

        // 3. Scheduler now stores $102 with priceLastUpdatedAt = block.timestamp - 2700
        // 4. Consumers calling getPricesNoOlderThan(subscriptionId, ids, 3600)
        //    receive $102 instead of the true ~$100.40
    }
}
```

The call succeeds because:
- `publishTime = block.timestamp − 2700 >= block.timestamp − 3600` ✓ (passes `TimestampTooOld` check)
- `publishTime > priceLastUpdatedAt` ✓ (passes `TimestampOlderThanLastUpdate` check)
- `|102 − 100| / 100 * 10000 = 200 bps >= 100 bps` ✓ (passes deviation check) [8](#0-7)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L373-386)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L413-450)
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```
