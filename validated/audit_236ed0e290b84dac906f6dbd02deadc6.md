### Title
Hardcoded `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` Blocks Equity-Feed Subscriptions During Market Closures — (`File: target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol`)

---

### Summary

`Scheduler.sol` applies a single hardcoded `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours` constant uniformly to every subscription when validating keeper-submitted price updates. This constant cannot be changed by the admin. For subscriptions containing equity price feeds (which have trading sessions), the last valid Pythnet timestamp is from the most recent market close — potentially many hours or days in the past. Any keeper calling `updatePriceFeeds()` on such a subscription outside market hours will receive a `TimestampTooOld` revert, making the subscription permanently un-updatable during market closures.

---

### Finding Description

`SchedulerConstants.sol` declares:

```solidity
uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
``` [1](#0-0) 

This constant is inherited by `Scheduler` and consumed in `_validateShouldUpdatePrices()`:

```solidity
uint256 minAllowedTimestamp = (block.timestamp > PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
    ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
    : 0;

if (updateTimestamp < minAllowedTimestamp) {
    revert SchedulerErrors.TimestampTooOld(updateTimestamp, block.timestamp);
}
``` [2](#0-1) 

The `updateTimestamp` is the **maximum** `publishTime` across all price feeds in the submitted update data. For a subscription containing only equity feeds (e.g., SPX, AAPL), when markets are closed, Pythnet's most recent price timestamp is from the last trading session — which can be 16+ hours ago on weekdays and 60+ hours ago after weekends. The 1-hour window unconditionally rejects these updates.

The code's own comment acknowledges the equity market problem but only partially addresses it:

> "We don't use this when parsing update data from the Pyth contract because we don't want to reject update data if it contains a price from a market that closed a few days ago." [3](#0-2) 

The `parsePriceFeedUpdatesWithConfig` call correctly uses `minPublishTime = 0`, but `_validateShouldUpdatePrices` then re-applies the 1-hour window to the maximum timestamp — defeating the intent for equity-only subscriptions. [4](#0-3) 

There is no admin setter for `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`. It is declared `public constant` and is not modifiable post-deployment. The admin can adjust `minimumBalancePerFeed` and `singleUpdateKeeperFeeInWei` via `SchedulerGovernance`, but not the timestamp validity window.

Similarly, `GAS_OVERHEAD = 30000` used in keeper fee calculation is also hardcoded:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
``` [5](#0-4) [6](#0-5) 

If actual transaction overhead diverges from 30,000 gas (e.g., after EVM opcode repricing or on different L2 chains), keepers are systematically under- or over-compensated.

---

### Impact Explanation

- **Equity-only subscriptions are permanently un-updatable during market closures** (~16.5 hours/weekday, ~60 hours over weekends). Keepers calling `updatePriceFeeds()` always revert with `TimestampTooOld`.
- Subscribers' ETH balances are locked in subscriptions that cannot fulfill their `updateOnHeartbeat` or `updateOnDeviation` criteria.
- Keepers waste gas on predictably-failing transactions, reducing keeper participation.
- The protocol cannot serve its stated use case for equity price feeds without a contract upgrade.

---

### Likelihood Explanation

High. US equity markets are closed ~16.5 hours per weekday and all weekend. Any subscription tracking equity-only feeds (SPX, AAPL, TSLA, etc.) will be blocked from updates for the majority of each calendar day. This is not a theoretical edge case — it is the normal operating state for equity feeds.

---

### Recommendation

1. Replace `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` with a mutable admin-configurable state variable (e.g., `pastTimestampMaxValidityPeriod`) with a governance setter in `SchedulerGovernance.sol`, analogous to how `minimumBalancePerFeed` and `singleUpdateKeeperFeeInWei` are already configurable.
2. Consider making the validity period **per-subscription** or **per-asset-class**, since crypto feeds (24/7) and equity feeds (session-based) have fundamentally different freshness characteristics.
3. Similarly, expose `GAS_OVERHEAD` as an admin-settable parameter to allow adjustment as gas costs evolve across chains and EVM upgrades.

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription with an equity price feed (e.g., SPX/USD).
2. Advance `block.timestamp` past market close by more than 1 hour (e.g., `vm.warp(block.timestamp + 2 hours)`).
3. Obtain valid Pythnet update data for the equity feed — the `publishTime` will be from the last trading session (>1 hour ago).
4. Call `scheduler.updatePriceFeeds(subscriptionId, updateData)` as any keeper.
5. Observe revert: `SchedulerErrors.TimestampTooOld(publishTime, block.timestamp)`.

The revert path is:

`updatePriceFeeds()` → `_validateShouldUpdatePrices()` → `updateTimestamp < block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` → `revert TimestampTooOld` [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L14-26)
```text
    /// Maximum time in the past (relative to current block timestamp)
    /// for which a price update timestamp is considered valid
    /// when validating the update conditions.
    /// @dev Note: We don't use this when parsing update data from the Pyth contract
    /// because don't want to reject update data if it contains a price from a market
    /// that closed a few days ago, since it will contain a timestamp from the last
    /// trading period. We enforce this value ourselves against the maximum
    /// timestamp in the provided update data.
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;

    /// Maximum time in the future (relative to current block timestamp)
    /// for which a price update timestamp is considered valid
    uint64 public constant FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD = 10 seconds;
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L29-29)
```text
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-347)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

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

        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }

        // Verify that update conditions are met, and that the timestamp
        // is more recent than latest stored update's. Reverts if not.
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L375-386)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L846-846)
```text
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
```
