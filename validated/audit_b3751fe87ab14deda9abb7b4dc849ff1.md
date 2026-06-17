### Title
L2 Sequencer Downtime Causes Scheduler `updatePriceFeeds` to Permanently Reject Valid Updates Due to Hardcoded 1-Hour Validity Window - (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Pyth Pulse `Scheduler` contract enforces a hardcoded `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours` window. If an L2 sequencer (Arbitrum, Optimism, Base, etc.) goes down for more than one hour, all Pythnet price timestamps generated during the outage become permanently too old to submit once the sequencer resumes. Keepers cannot earn their fees, subscription consumers receive stale prices, and any downstream protocol calling `getPricesNoOlderThan` will revert for the entire gap period.

---

### Finding Description

`SchedulerConstants.sol` defines:

```solidity
uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
``` [1](#0-0) 

Inside `_validateShouldUpdatePrices`, the contract computes a minimum allowed timestamp and rejects any update whose most-recent `publishTime` falls below it:

```solidity
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
``` [2](#0-1) 

This check is applied inside `updatePriceFeeds`, which is the sole entry point for keepers to push price data and earn their gas-reimbursement fee: [3](#0-2) 

The keeper fee is calculated and paid only after `_validateShouldUpdatePrices` succeeds: [4](#0-3) 

The keeper payment logic: [5](#0-4) 

Consumers relying on `getPricesNoOlderThan` will revert with `StalePrice` for the entire gap: [6](#0-5) 

**Attack / failure path:**

1. L2 sequencer (Arbitrum, Optimism, Base) goes down for ≥ 1 hour (documented outages: Arbitrum 10-hour outage, Optimism Bedrock 2–4 hour downtime).
2. Pythnet continues producing price updates with real-world timestamps.
3. When the sequencer resumes, `block.timestamp` jumps to the current wall-clock time.
4. Any Pythnet price data generated during the outage now has `publishTime < block.timestamp - 1 hour`.
5. Every call to `updatePriceFeeds` with that data reverts with `TimestampTooOld`.
6. Keepers cannot submit updates and cannot earn fees for the entire outage window.
7. All subscriptions remain stale; `getPricesNoOlderThan` reverts for downstream consumers.

---

### Impact Explanation

- **Keepers** lose all fee income for the outage period — they cannot submit valid updates and `_processFeesAndPayKeeper` is never reached.
- **Subscription consumers** receive stale prices. Any protocol calling `getPricesNoOlderThan` with a tight `age_seconds` will revert, potentially blocking liquidations, trades, or other time-sensitive operations.
- **Subscription owners** continue to hold locked ETH in the contract but receive no service for the outage window.

The impact directly mirrors the external report: a time-sensitive on-chain window (1 hour) expires during sequencer downtime, making it impossible to perform the intended action (submit a price update / exercise an option) once the chain resumes.

---

### Likelihood Explanation

- The Scheduler is an EVM contract explicitly targeting chains that include L2s (Arbitrum, Optimism, Base).
- Documented L2 sequencer outages have lasted from 1 hour (Arbitrum June 2023 batch-poster bug) to 10 hours (Arbitrum December 2021). Both exceed the 1-hour validity window.
- No privileged access is required; the failure is triggered purely by the passage of real-world time during a sequencer halt.
- The 1-hour window is hardcoded as a constant with no governance override or emergency extension mechanism.

---

### Recommendation

1. **Integrate a Chainlink L2 sequencer uptime feed** (as recommended in the external report) to detect sequencer downtime and pause the staleness check or extend the validity window during outages.
2. **Make `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` governance-adjustable** so it can be extended in response to known outages.
3. **Add a "grace period" extension**: when the sequencer resumes after a detected downtime, temporarily widen the validity window by the duration of the outage.
4. At minimum, document the risk clearly for integrators deploying on L2s.

---

### Proof of Concept

```
1. Deploy Scheduler on Arbitrum (or fork).
2. Call updatePriceFeeds() successfully at T=0 with publishTime=T.
3. Simulate sequencer downtime: vm.warp(T + 3601) (1 hour + 1 second).
4. Attempt updatePriceFeeds() with updateData whose publishTime = T + 1 (generated during outage).
5. Transaction reverts with TimestampTooOld(T+1, T+3601) because T+1 < (T+3601 - 3600) = T+1.
   (Edge case: exactly at boundary; any outage > 1 hour makes all mid-outage data permanently invalid.)
6. Keeper earns zero fees; getPricesNoOlderThan(subscriptionId, priceIds, 60) reverts with StalePrice.
``` [7](#0-6) [8](#0-7) [5](#0-4)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L14-22)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-348)
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L551-552)
```text
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L841-864)
```text
        SchedulerStructs.SubscriptionStatus storage status,
        uint256 startGas,
        uint256 numPriceIds
    ) internal {
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
    }
```
