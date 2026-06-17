### Title
Scheduler `updatePriceFeeds()` Staleness Check Uses Maximum Timestamp Across All Feeds, Allowing Arbitrarily Old Prices to Be Stored and Served as Fresh — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updatePriceFeeds()` validates staleness using only the **maximum** `publishTime` across all feeds in a batch. An unprivileged keeper can submit a batch containing a fresh price for one feed and an arbitrarily old (but validly signed) price for another feed. The staleness check passes using the fresh feed's timestamp, the old price is stored, and `getPricesNoOlderThan()` subsequently serves that stale price to readers as if it were fresh — because it also checks only the subscription-level max timestamp, not individual feed timestamps.

---

### Finding Description

**Root cause — `minPublishTime = 0` in `parsePriceFeedUpdatesWithConfig`:**

`updatePriceFeeds()` calls the Pyth core contract with `minPublishTime = 0`, explicitly accepting any historical price:

```solidity
pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
    updateData,
    params.priceIds,
    0,   // ← no lower bound on individual feed timestamps
    curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
    false, true, false
);
``` [1](#0-0) 

**Root cause — `_validateShouldUpdatePrices` uses only the max timestamp:**

The staleness check computes `updateTimestamp` as the maximum `publishTime` across all feeds, then validates only that maximum against the 1-hour window:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
// ...
if (updateTimestamp < minAllowedTimestamp) {
    revert SchedulerErrors.TimestampTooOld(...);
}
``` [2](#0-1) 

Individual feed timestamps are never validated. A feed with a `publishTime` of one year ago passes as long as another feed in the same batch has a fresh timestamp.

**Root cause — `getPricesNoOlderThan()` checks the subscription-level max timestamp, not individual feed timestamps:**

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) 

`status.priceLastUpdatedAt` is set to the max timestamp from the last update:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
``` [4](#0-3) 

A reader calling `getPricesNoOlderThan(subscriptionId, [ETH_USD], 60)` receives a price that could be hours or years old for ETH/USD, because the 60-second check is satisfied by a different feed's fresh timestamp.

**`_storePriceUpdates` overwrites all feeds unconditionally:**

```solidity
for (uint8 i = 0; i < priceFeeds.length; i++) {
    _state.priceUpdates[subscriptionId][priceFeeds[i].id] = priceFeeds[i];
}
``` [5](#0-4) 

There is no per-feed timestamp guard preventing an old price from overwriting a fresher stored price.

**The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` constant is 1 hour, but only applied to the max timestamp:** [6](#0-5) 

---

### Impact Explanation

A downstream protocol (lending, perps, liquidation engine) that reads prices from the Scheduler via `getPricesNoOlderThan()` with a strict age parameter (e.g., 60 seconds) will receive a price that is arbitrarily old for a targeted feed, while the staleness guard silently passes. This enables:

- **Incorrect liquidations**: An attacker stores a historically low price for a collateral asset; the lending protocol liquidates a healthy position.
- **Undercollateralized borrowing**: An attacker stores a historically high price for a collateral asset; the lending protocol allows excessive borrowing.
- **Denial of valid liquidations**: An attacker stores a historically high price for a debt asset; the lending protocol refuses to liquidate an insolvent position.

The impact is direct financial loss to users of any protocol that integrates the Scheduler and relies on `getPricesNoOlderThan()` for safety-critical decisions.

---

### Likelihood Explanation

- `updatePriceFeeds()` has **no access control** — any address can act as a keeper.
- Historical Wormhole-signed VAAs for any price feed are publicly available from the Hermes Benchmarks API (`/v1/updates/price/{timestamp}`), so the attacker does not need to forge any data.
- The attack requires only one subscription with two or more feeds, which is the normal use case.
- The `TimestampOlderThanLastUpdate` check only prevents the max timestamp from going backward; it does not prevent individual feed timestamps from being old.
- On the **first update** (`status.priceLastUpdatedAt == 0`), the check is skipped entirely, making the attack trivially executable on any new subscription.

---

### Recommendation

1. **Per-feed timestamp validation in `_validateShouldUpdatePrices` or `_storePriceUpdates`**: Reject any individual feed whose `publishTime` is older than `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` (unless it is a known closed-market feed, handled separately).

2. **Fix `getPricesNoOlderThan()` to check individual feed timestamps**: Instead of checking only `status.priceLastUpdatedAt`, iterate over the returned feeds and verify each feed's `publishTime` satisfies the `age_seconds` constraint:

```solidity
for (uint i = 0; i < prices.length; i++) {
    if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
        revert PythErrors.StalePrice();
}
```

3. **Pass a non-zero `minPublishTime`** to `parsePriceFeedUpdatesWithConfig` equal to `block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`, so the Pyth core contract itself rejects stale individual feeds before they reach the Scheduler's storage.

---

### Proof of Concept

1. Create a Scheduler subscription with two feeds: `ETH/USD` (Feed A) and `BTC/USD` (Feed B).
2. From the Hermes Benchmarks API, fetch a valid Wormhole-signed VAA for `ETH/USD` from one year ago when ETH was at $1,000.
3. Fetch a fresh VAA for `BTC/USD` at the current price.
4. Call `Scheduler.updatePriceFeeds(subscriptionId, [freshBTC, oldETH])`.
5. Inside `_validateShouldUpdatePrices`: `updateTimestamp = max(BTC.publishTime, ETH.publishTime) = BTC.publishTime` (fresh). The 1-hour staleness check passes. The `TimestampOlderThanLastUpdate` check passes (first update, `priceLastUpdatedAt == 0`).
6. `_storePriceUpdates` stores ETH/USD at $1,000 with a 1-year-old `publishTime`.
7. `status.priceLastUpdatedAt = BTC.publishTime` (fresh).
8. A lending protocol calls `getPricesNoOlderThan(subscriptionId, [ETH_USD], 60)`.
9. The check: `distance(block.timestamp, status.priceLastUpdatedAt) = distance(now, BTC.publishTime) ≤ 60` — **passes**.
10. Returns ETH/USD = $1,000 (1 year old). The lending protocol believes ETH is worth $1,000 and allows a user to borrow 1,000× more than they should, or liquidates a healthy ETH-collateralized position.

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L366-386)
```text
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
        }

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L551-554)
```text
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L827-831)
```text
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            _state.priceUpdates[subscriptionId][priceFeeds[i].id] = priceFeeds[
                i
            ];
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```
