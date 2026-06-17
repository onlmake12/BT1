Let me search the Pyth codebase for fee/balance accounting patterns that could have a similar stale-read vulnerability.

### Title
Stale Pre-Deduction Balance Check in `updatePriceFeeds` Allows Keeper DoS When Subscription Balance Falls Between `pythFee` and `pythFee + keeperFee` — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updatePriceFeeds` performs an initial balance check only against `pythFee`, then deducts `pythFee` from `status.balanceInWei`, and only afterward checks the now-reduced balance against `totalKeeperFee` inside `_processFeesAndPayKeeper`. When a subscription's balance falls in the range `[pythFee, pythFee + totalKeeperFee)`, the initial check passes, `pythFee` is deducted, and the keeper-fee check reverts — causing the entire transaction to roll back. Keepers cannot update the subscription, price feeds go stale, and the keeper wastes gas on every attempt.

---

### Finding Description

In `Scheduler.sol`, `updatePriceFeeds` executes the following sequence:

1. **Line 292**: `pythFee = pyth.getUpdateFee(updateData)` — compute Pyth fee.
2. **Line 295–297**: `if (status.balanceInWei < pythFee) revert` — check only against `pythFee`.
3. **Line 305**: `status.balanceInWei -= pythFee` — deduct `pythFee` from balance.
4. **Line 311**: `pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(...)` — external call consuming `pythFee`.
5. **Line 345**: `_processFeesAndPayKeeper(status, startGas, params.priceIds.length)` — compute and deduct keeper fee.

Inside `_processFeesAndPayKeeper` (lines 840–864):

```solidity
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
status.balanceInWei -= totalKeeperFee;
```

At this point `status.balanceInWei` has already been reduced by `pythFee`. If the original balance was in the range `[pythFee, pythFee + totalKeeperFee)`, the initial check at line 295 passes, `pythFee` is deducted, and then the keeper-fee check at line 852 reverts. Because the entire transaction reverts, the state rolls back — but the keeper has wasted gas and the subscription remains un-updatable until the manager adds funds.

The root cause is identical in structure to M-12: a balance is read and partially validated before a fee-deducting operation, and the post-deduction balance is then checked against a second fee — but the initial check did not account for the combined total. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

When a subscription's balance naturally depletes to the range `[pythFee, pythFee + totalKeeperFee)`:

- Every keeper call to `updatePriceFeeds` reverts after consuming gas.
- The subscription's price feeds become permanently stale until the manager manually calls `addFunds`.
- Downstream consumers of `getPricesNoOlderThan` / `getEmaPricesNoOlderThan` will revert with `StalePrice`, breaking any protocol that depends on those feeds.
- A malicious subscription manager can deliberately fund a subscription to exactly `pythFee` to grief keepers into wasting gas on repeated failed transactions. [3](#0-2) 

---

### Likelihood Explanation

This is a natural steady-state condition. Every active subscription's balance decreases with each update. Once the balance crosses below `pythFee + totalKeeperFee` but remains above `pythFee`, the subscription enters the un-updatable zone. Because `totalKeeperFee` includes a gas-cost component (`(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice`) that varies with gas price, the threshold shifts dynamically — making it easy to cross unintentionally during high-gas-price periods. No privileged access is required; any keeper calling `updatePriceFeeds` triggers the revert. [4](#0-3) 

---

### Recommendation

Perform a combined upfront balance check before deducting `pythFee`. Since `totalKeeperFee` depends on gas consumed during the call, use a conservative lower bound (e.g., `GAS_OVERHEAD * tx.gasprice + singleUpdateKeeperFeeInWei * numPriceIds`) for the pre-flight check:

```solidity
uint256 minKeeperFee = (GAS_OVERHEAD * tx.gasprice) +
    uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length;

if (status.balanceInWei < pythFee + minKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This mirrors the recommendation in M-12: re-fetch (or correctly account for) the balance after all intermediate deductions before computing the final fee. [5](#0-4) 

---

### Proof of Concept

Setup:
- `pythFee = 1000 wei` (e.g., 2 price feeds × 500 wei each)
- `GAS_OVERHEAD = 100_000`, `tx.gasprice = 10 gwei`, `singleUpdateKeeperFeeInWei = 0`
- `minKeeperFee ≈ 100_000 × 10 gwei = 1_000_000 gwei = 0.001 ETH`
- Subscription balance = `1001 wei` (passes `balanceInWei >= pythFee` check)

Execution:
1. Line 295: `1001 >= 1000` → check passes.
2. Line 305: `status.balanceInWei = 1001 - 1000 = 1 wei`.
3. Line 311: Pyth call succeeds (pythFee paid).
4. Line 345 → line 852: `1 wei < 0.001 ETH` → `revert InsufficientBalance`.
5. Entire transaction reverts. Keeper loses gas. Subscription price feeds are not updated.

The subscription is now permanently un-updatable until the manager calls `addFunds`, and any downstream call to `getPricesNoOlderThan` will revert with `StalePrice`. [6](#0-5) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-863)
```text
    function _processFeesAndPayKeeper(
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
```
