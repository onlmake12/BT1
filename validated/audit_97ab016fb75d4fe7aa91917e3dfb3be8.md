### Title
Strict `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` Freezes Equity-Only Pulse Subscriptions During Regular Market Closures — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler.sol` contract enforces a hardcoded 1-hour validity window (`PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours`) on the maximum `publishTime` across all price feeds in a subscription update. For subscriptions containing only equity price feeds (e.g., AAPL/USD, TSLA/USD), Pyth publishes prices with timestamps from the last trading session when markets are closed. During weekends and holidays, all equity feed timestamps are 48–72+ hours old. The `TimestampTooOld` check in `_validateShouldUpdatePrices` rejects every `updatePriceFeeds` call, permanently freezing the subscription for the entire closure period. Any DApp relying on `getPricesNoOlderThan` for such a subscription will receive `StalePrice` reverts for the duration.

---

### Finding Description

**Root cause — `SchedulerConstants.sol` line 22:**

```solidity
uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```

**Vulnerable check — `Scheduler.sol` `_validateShouldUpdatePrices`, lines 366–386:**

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}

uint256 minAllowedTimestamp = (block.timestamp >
    PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
    ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
    : 0;

if (updateTimestamp < minAllowedTimestamp) {
    revert SchedulerErrors.TimestampTooOld(
        updateTimestamp,
        block.timestamp
    );
}
```

The `updateTimestamp` is the **maximum** `publishTime` across all feeds in the subscription. The code comment in `updatePriceFeeds` (lines 299–304) explicitly acknowledges that Pyth publishes prices with timestamps from the last trading period for closed markets:

> *"We don't want to reject update data if it contains a price from a market that closed a few days ago, since it will contain a timestamp from the last trading period."*

This is why `parsePriceFeedUpdatesWithConfig` is called with `minPublishTime = 0`. However, the Scheduler then applies its own stricter 1-hour check in `_validateShouldUpdatePrices`. For a subscription containing **only** equity feeds, the MAX timestamp during a weekend is from Friday's market close — potentially 48–72 hours old. The 1-hour check rejects every update attempt for the entire weekend.

The `updatePriceFeeds` function also deducts the Pyth fee from `status.balanceInWei` **before** calling `_validateShouldUpdatePrices` (line 305), meaning keepers lose their Pyth fee on every failed attempt during the freeze period.

---

### Impact Explanation

1. All `updatePriceFeeds` calls for equity-only subscriptions revert with `TimestampTooOld` during weekends and holidays.
2. `getPricesNoOlderThan` reverts with `StalePrice` for any age requirement shorter than the market closure duration (48–72+ hours).
3. DApps relying on these subscriptions for settlement, liquidation, or risk management are completely broken during regular market closures.
4. Keeper balance is drained by repeated failed attempts (Pyth fee is deducted before the timestamp check).
5. Permanent subscriptions (`isPermanent = true`) cannot be updated by the manager to work around the issue.

---

### Likelihood Explanation

- Equity markets are closed every Saturday and Sunday — this freeze occurs **every single weekend** for equity-only subscriptions.
- It also occurs on market holidays (e.g., Christmas, New Year's Day, Thanksgiving).
- Any user who creates a Pulse subscription with only equity price feeds (a documented and supported use case) will experience this.
- No attacker action is required; the freeze is a predictable, regular consequence of normal market operation.

---

### Recommendation

1. Increase `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` to accommodate equity market closures (e.g., 72 hours or more), or make it a per-subscription configurable parameter.
2. Alternatively, apply the `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` check only to feeds whose markets are expected to be open (e.g., crypto feeds), and skip it for feeds with known market-hours schedules.
3. Move the Pyth fee deduction (`status.balanceInWei -= pythFee`) to **after** `_validateShouldUpdatePrices` to prevent keeper balance drain on predictably failing calls.

---

### Proof of Concept

1. Create a Pulse subscription with `updateOnHeartbeat = true, heartbeatSeconds = 3600` and price IDs for equity feeds only (e.g., AAPL/USD, TSLA/USD).
2. Fund the subscription and perform the first update on a Friday during trading hours — succeeds.
3. On Saturday, fetch the latest available Pyth update data for the equity feeds. The `publishTime` in the data is from Friday's close (~24+ hours ago).
4. Call `updatePriceFeeds` with this data.
5. Inside `_validateShouldUpdatePrices`, `updateTimestamp` = Friday's close timestamp. `minAllowedTimestamp` = `block.timestamp - 3600`. Since Friday's close is > 1 hour ago, the check at line 381 fires: `revert TimestampTooOld(fridayCloseTimestamp, block.timestamp)`.
6. The subscription remains frozen. `getPricesNoOlderThan(subscriptionId, priceIds, 3600)` reverts with `StalePrice` for the entire weekend.
7. The keeper's Pyth fee was already deducted at line 305 before the revert, draining the subscription balance on every retry. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L299-319)
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
            );
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
