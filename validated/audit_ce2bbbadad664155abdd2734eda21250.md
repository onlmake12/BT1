### Title
`updatePriceFeeds` Initial Balance Check Uses Only `pythFee` Instead of Total Required Fee (`pythFee + keeperFee`) - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.updatePriceFeeds`, the upfront balance check validates only against `pythFee` (the Pyth oracle update fee), not against the total required balance of `pythFee + keeperFee`. This mirrors the M-8 pattern: using an incomplete/wrong variable for the fee sufficiency check. A subscription whose balance satisfies `pythFee ≤ balance < pythFee + keeperFee` passes the initial guard, the Pyth oracle is called with ETH, and only then does the keeper-fee check in `_processFeesAndPayKeeper` revert the entire transaction. Because the revert is late, keepers waste gas on transactions that are structurally doomed to fail.

---

### Finding Description

In `updatePriceFeeds`, the guard at line 295 checks only the Pyth oracle fee:

```solidity
// If we don't have enough balance, revert
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [1](#0-0) 

Immediately after, the Pyth fee is deducted and the oracle is called:

```solidity
status.balanceInWei -= pythFee;
status.totalSpent += pythFee;
...
pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(...);
``` [2](#0-1) 

Only at the very end of the function is the keeper fee computed and checked:

```solidity
function _processFeesAndPayKeeper(...) internal {
    uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
    uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
    uint256 totalKeeperFee = gasCost + keeperSpecificFee;

    if (status.balanceInWei < totalKeeperFee) {
        revert SchedulerErrors.InsufficientBalance();
    }
``` [3](#0-2) 

The total balance required to complete a successful update is `pythFee + totalKeeperFee`, but the initial guard only checks `pythFee`. When `pythFee ≤ balance < pythFee + totalKeeperFee`, the transaction proceeds through the Pyth oracle call, all validation logic, and price storage, before reverting at the keeper-fee check. Because Solidity reverts unwind all state changes and ETH transfers, no funds are permanently lost — but the keeper has already consumed gas for the entire execution path.

---

### Impact Explanation

Any permissionless keeper calling `updatePriceFeeds` on a subscription whose balance falls in the range `[pythFee, pythFee + totalKeeperFee)` will have their transaction revert after consuming the full gas cost of the function. The subscription cannot be updated, and the keeper has no on-chain way to detect this condition before submitting the transaction (the keeper fee is dynamic, based on actual gas consumed). Subscriptions in this balance range are silently stuck: the initial check does not revert early, so off-chain monitoring tools that simulate only the initial guard will incorrectly predict success.

---

### Likelihood Explanation

This condition arises naturally as a subscription's balance drains over time through repeated updates. When the balance drops below `pythFee + keeperFee` but remains above `pythFee`, every keeper attempt reverts. The subscription manager may not immediately understand why updates have stopped, since the balance appears non-zero and above the Pyth fee threshold. Any permissionless keeper can trigger this path with no special privileges.

---

### Recommendation

Add a minimum keeper-fee estimate to the upfront balance check. The minimum keeper fee is computable before execution as `GAS_OVERHEAD * tx.gasprice + singleUpdateKeeperFeeInWei * numPriceIds`:

```solidity
uint256 minKeeperFee = (GAS_OVERHEAD * tx.gasprice) +
    (uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length);

if (status.balanceInWei < pythFee + minKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This mirrors the fix in M-8: replace the check against the incomplete variable (`pythFee` alone) with a check against the full required amount (`pythFee + minKeeperFee`), so the revert is early and keepers do not waste gas on structurally doomed transactions.

---

### Proof of Concept

1. A subscription is created with `balance = pythFee + singleUpdateKeeperFeeInWei * numPriceIds` (just enough for the fixed keeper component, but not the gas-cost component).
2. A keeper calls `updatePriceFeeds`.
3. Line 295 check passes: `balance >= pythFee`. ✓
4. `status.balanceInWei -= pythFee` executes; `pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}` is called.
5. All validation and storage logic executes.
6. `_processFeesAndPayKeeper` computes `totalKeeperFee = gasCost + keeperSpecificFee`. Since `gasCost > 0`, `totalKeeperFee > keeperSpecificFee`, and `status.balanceInWei` (now `balance - pythFee`) is less than `totalKeeperFee`.
7. `revert SchedulerErrors.InsufficientBalance()` fires — the entire transaction reverts, consuming the keeper's gas.
8. The subscription remains un-updated; the keeper has no early signal that the transaction would fail. [4](#0-3) [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-864)
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
    }
```
