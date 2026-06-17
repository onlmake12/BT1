### Title
`getPricesNoOlderThan` / `getEmaPricesNoOlderThan` Create False Sense of Data Freshness via MAX Timestamp — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `_validateShouldUpdatePrices` selects the **maximum** `publishTime` across all price feeds in a batch and stores it as `status.priceLastUpdatedAt`. Both `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` then validate freshness against this single MAX value. When a subscription contains feeds for both active markets (fresh timestamps) and closed markets (stale timestamps from the last trading period), the freshness check passes for the entire batch, and consumers receive stale prices with no revert — a direct analog to the reported `EACAggregatorCombine` issue.

---

### Finding Description

In `_validateShouldUpdatePrices`, the representative timestamp for the entire update batch is computed as the maximum individual feed `publishTime`:

```solidity
// Scheduler.sol lines 366–371
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [1](#0-0) 

This MAX value is then stored as the subscription-level freshness marker:

```solidity
// Scheduler.sol line 340
status.priceLastUpdatedAt = latestPublishTime;
``` [2](#0-1) 

Both `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` validate freshness exclusively against this single stored value:

```solidity
// Scheduler.sol lines 551–552
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
``` [3](#0-2) [4](#0-3) 

The code's own comment acknowledges the design intent — closed markets retain their last-trading-period timestamp — but does not account for the false freshness guarantee this creates for consumers of `getPricesNoOlderThan`:

```
// Use the most recent timestamp, as some asset markets may be closed.
// Closed markets will have a publishTime from their last trading period.
``` [5](#0-4) 

The staleness guard at line 381 also uses this MAX, so a batch containing one fresh feed and one days-old closed-market feed passes the `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` (1 hour) check: [6](#0-5) [7](#0-6) 

---

### Impact Explanation

A protocol subscribing to a mixed set of feeds (e.g., BTC/USD + GOLD/USD) and calling `getPricesNoOlderThan(subscriptionId, priceIds, 60)` will receive no revert even when the GOLD/USD price is days old (market closed), because `priceLastUpdatedAt` reflects only BTC/USD's fresh timestamp. The returned `PythStructs.Price` structs carry individual `publishTime` fields, but the entire purpose of `getPricesNoOlderThan` is to spare the caller from checking those — the function's name and interface imply a per-feed freshness guarantee that is not actually enforced. Downstream protocols (lending, derivatives, structured products) that rely on this guarantee may execute trades, liquidations, or settlements against stale closed-market prices.

---

### Likelihood Explanation

This is reachable by any unprivileged keeper or reader interacting with a subscription that mixes active-market and closed-market feeds — a common and explicitly supported use case per the contract's own comments. No privileged access, key compromise, or external oracle manipulation is required. The keeper simply submits a valid same-slot update that happens to include a closed-market feed with a stale `publishTime`, which the contract explicitly allows (minimum publish time is set to `0` during parsing). [8](#0-7) 

---

### Recommendation

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` should validate freshness against each individual feed's `publishTime`, not the subscription-level MAX. Concretely, after retrieving the feeds via `_getPricesInternal`, iterate and check each `price.publishTime` against `age_seconds` before returning. Alternatively, document clearly that these functions do **not** guarantee per-feed freshness and that callers must inspect individual `publishTime` fields.

---

### Proof of Concept

1. Manager creates a subscription with two price IDs: `BTC_USD` (active market) and `GOLD_USD` (closed market).
2. Keeper calls `updatePriceFeeds` with a same-slot batch where:
   - `BTC_USD.publishTime = block.timestamp` (fresh)
   - `GOLD_USD.publishTime = block.timestamp - 2 days` (stale, last trading period)
3. `_validateShouldUpdatePrices` computes `updateTimestamp = block.timestamp` (MAX), passes the 1-hour staleness guard, and stores `priceLastUpdatedAt = block.timestamp`.
4. Reader calls `getPricesNoOlderThan(subscriptionId, [GOLD_USD], 60)`.
5. Check: `distance(block.timestamp, block.timestamp) = 0 ≤ 60` → **no revert**.
6. Reader receives GOLD/USD price with `publishTime = block.timestamp - 2 days`, falsely believing it is no older than 60 seconds.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L299-318)
```text
        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L362-365)
```text
        // Use the most recent timestamp, as some asset markets may be closed.
        // Closed markets will have a publishTime from their last trading period.
        // Since we verify all updates share the same Pythnet slot, we still ensure
        // that all price feeds are synchronized from the same update cycle.
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L380-386)
```text
        // Validate that the update timestamp is not too old
        if (updateTimestamp < minAllowedTimestamp) {
            revert SchedulerErrors.TimestampTooOld(
                updateTimestamp,
                block.timestamp
            );
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L549-554)
```text
        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L592-597)
```text
        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```
