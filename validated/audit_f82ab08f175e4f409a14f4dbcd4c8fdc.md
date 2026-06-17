### Title
Split Fee Validation Allows Subscription Updates to Revert After Pyth Fee Deduction - (File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol)

### Summary

In `Scheduler.sol`, the `updatePriceFeeds` function validates the subscription balance against the Pyth fee and the keeper fee in two **separate, sequential checks** rather than against their combined total. The Pyth fee is deducted from `status.balanceInWei` before the keeper fee is ever evaluated. When a subscription's balance satisfies `pythFee <= balance < pythFee + totalKeeperFee`, the function passes the first check, deducts the Pyth fee, and then reverts inside `_processFeesAndPayKeeper`. This mirrors the exact vulnerability class in the external report: independently-set fee components are never validated as a combined total before funds are committed.

### Finding Description

`updatePriceFeeds` performs two independent balance checks:

**Check 1** (line 295): validates only against `pythFee`
```solidity
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

**Deduction** (line 305): Pyth fee is immediately subtracted and ETH is forwarded:
```solidity
status.balanceInWei -= pythFee;
...
pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(...)
```

**Check 2** (line 852, inside `_processFeesAndPayKeeper`): validates remaining balance against `totalKeeperFee` only *after* the Pyth fee has already been deducted:
```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

The three fee components — `pythFee` (set by the Pyth contract), `gasCost` (dynamic, based on `tx.gasprice`), and `keeperSpecificFee` (set by admin via `singleUpdateKeeperFeeInWei`) — are set independently and are never validated as a combined total before the first deduction occurs. [1](#0-0) [2](#0-1) 

### Impact Explanation

When `pythFee <= status.balanceInWei < pythFee + totalKeeperFee`:

1. The first check passes.
2. `status.balanceInWei -= pythFee` executes and ETH is forwarded to the Pyth contract.
3. `_processFeesAndPayKeeper` reverts with `InsufficientBalance`.
4. The entire transaction reverts (EVM unwinds all state changes including the ETH transfer).

The direct consequence is that **any keeper calling `updatePriceFeeds` on such a subscription will always have their transaction revert**, wasting gas. The subscription's price feeds cannot be updated until the owner tops up the balance. If `tx.gasprice` spikes (e.g., during network congestion), `gasCost` grows and subscriptions that were previously updatable enter this broken state without any on-chain warning. This is a **temporary DoS on price feed updates** for affected subscriptions. [3](#0-2) 

### Likelihood Explanation

The `gasCost` component of `totalKeeperFee` is dynamic: `(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice`. During gas price spikes, `totalKeeperFee` can grow significantly beyond what was anticipated when the subscription was funded. A subscription funded to exactly cover `pythFee + keeperSpecificFee` (the predictable components) will fail whenever `gasCost` pushes the total above the remaining balance. This is a realistic, externally-triggerable condition requiring no privileged access — any keeper can call `updatePriceFeeds`. [4](#0-3) 

### Recommendation

Compute the combined fee requirement upfront before any deduction, and validate the balance against the total in a single check:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
uint256 estimatedKeeperFee = (GAS_OVERHEAD * tx.gasprice) +
    (uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length);
uint256 totalRequired = pythFee + estimatedKeeperFee;

if (status.balanceInWei < totalRequired) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This mirrors the pattern already used in `getMinimumBalance` and ensures the balance is sufficient for all fee components before any funds are committed. [5](#0-4) 

### Proof of Concept

```solidity
function test_updateRevertsWhenBalanceCoversPythFeeButNotKeeperFee() public {
    // Set a high gas price to inflate gasCost
    uint256 gasPrice = 100 gwei;
    vm.txGasPrice(gasPrice);

    uint256 subscriptionId = addTestSubscription(scheduler, address(reader));
    bytes32[] memory priceIds = createPriceIds();

    uint256 pythFee = MOCK_PYTH_FEE_PER_FEED * priceIds.length;
    // Fund with only pythFee — passes first check, fails keeper check
    scheduler.addFunds{value: pythFee}(subscriptionId);

    uint64 publishTime = SafeCast.toUint64(block.timestamp);
    PythStructs.PriceFeed[] memory priceFeeds;
    uint64[] memory slots;
    (priceFeeds, slots) = createMockPriceFeedsWithSlots(publishTime, priceIds.length);
    mockParsePriceFeedUpdatesWithSlotsStrict(pyth, priceFeeds, slots);
    bytes[] memory updateData = createMockUpdateData(priceFeeds);

    // Keeper call reverts — subscription cannot be updated
    vm.prank(pusher);
    vm.expectRevert(SchedulerErrors.InsufficientBalance.selector);
    scheduler.updatePriceFeeds(subscriptionId, updateData);
}
``` [6](#0-5) [5](#0-4)

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
