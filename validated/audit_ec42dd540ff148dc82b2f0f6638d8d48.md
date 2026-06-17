### Title
Unprotected `Scheduler::updatePriceFeeds` Allows Caller-Inflated `updateData` to Drain Subscription Balance — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updatePriceFeeds` is callable by any address with no access control. The caller fully controls the `updateData` array. The Pyth fee charged to the subscription is computed as `pyth.getUpdateFee(updateData)`, which scales linearly with the number of entries in `updateData`. Because there is no validation that `updateData.length` matches the subscription's `priceIds.length`, an attacker can pad `updateData` with arbitrarily many extra valid Pyth price-update entries. The subscription is charged for all of them, while only the subscription's own price IDs are actually consumed. The excess fee is permanently transferred to the Pyth protocol, draining the subscription's balance far faster than the owner intended.

---

### Finding Description

`updatePriceFeeds` records the Pyth fee before parsing:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);   // scales with updateData.length
if (status.balanceInWei < pythFee) revert ...;
status.balanceInWei -= pythFee;                    // full inflated amount deducted
...
pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
    updateData,
    params.priceIds,   // only subscription's N feeds are returned
    ...
);
``` [1](#0-0) 

`getUpdateFee` charges per entry in `updateData`. `parsePriceFeedUpdatesWithConfig` filters by `params.priceIds` and silently ignores extra entries. There is no assertion that `updateData.length == params.priceIds.length` anywhere in `updatePriceFeeds`. [2](#0-1) 

The keeper fee paid back to `msg.sender` is computed from `params.priceIds.length` (fixed), not from `updateData.length`, so the attacker's reward is unchanged while the subscription's net loss grows with every extra entry supplied. [3](#0-2) 

---

### Impact Explanation

A subscription owner deposits ETH expecting their balance to be consumed at a predictable rate (N feeds × singleUpdateFee per update cycle). An attacker who pads `updateData` with K extra entries causes the subscription to pay `(N + K) × singleUpdateFee` per call instead of `N × singleUpdateFee`. With K chosen to be large, a single malicious `updatePriceFeeds` call can exhaust the subscription's entire balance in one transaction. Once the balance falls below `getMinimumBalance`, the subscription becomes underfunded and legitimate keepers can no longer update it, permanently denying price-feed service to the subscription owner and any whitelisted readers who depend on it. [4](#0-3) 

---

### Likelihood Explanation

The function is intentionally permissionless — any address may call it. No special role, leaked key, or governance majority is required. The attacker only needs to obtain valid Pyth-signed update data for arbitrary price feeds (freely available from the Hermes API) and bundle them alongside the subscription's required feeds. The attack is cheap: the attacker earns the keeper fee (reimbursing most of their gas), while the victim subscription loses the full inflated Pyth fee. Any subscription with a meaningful balance is a profitable griefing target. [5](#0-4) 

---

### Recommendation

Add a length guard before computing the Pyth fee:

```solidity
require(
    updateData.length == params.priceIds.length,
    "updateData length must match subscription priceIds length"
);
```

Alternatively, compute the expected fee directly from `params.priceIds.length` using the single-update fee constant rather than delegating to `pyth.getUpdateFee(updateData)`, and pass only that fixed amount to `parsePriceFeedUpdatesWithConfig`. [6](#0-5) 

---

### Proof of Concept

1. A subscription exists with 2 price IDs (`params.priceIds.length == 2`) and a balance of 1 ETH.
2. The attacker fetches 1000 valid Pyth-signed price updates from Hermes (for arbitrary price IDs, including the 2 required ones).
3. The attacker calls `scheduler.updatePriceFeeds(subscriptionId, updateData)` where `updateData` contains all 1000 entries.
4. `pythFee = pyth.getUpdateFee(updateData)` returns `1000 × singleUpdateFee`.
5. `status.balanceInWei -= pythFee` deducts the full 1000-entry fee from the subscription.
6. `parsePriceFeedUpdatesWithConfig` returns only the 2 feeds matching `params.priceIds`; the other 998 entries are silently discarded.
7. The subscription's balance is drained by 500× the expected amount in a single call.
8. The subscription falls below `getMinimumBalance` and can no longer be updated by legitimate keepers. [1](#0-0)

### Citations

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
