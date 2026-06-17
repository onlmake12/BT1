### Title
Insufficient Pre-flight Balance Check in `updatePriceFeeds` Causes Valid Updates to Revert — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol::updatePriceFeeds()`, the initial balance guard only validates `status.balanceInWei >= pythFee`. The balance is then **immediately reduced** by `pythFee` before `_processFeesAndPayKeeper()` checks the now-reduced balance against `totalKeeperFee`. When a subscription's balance falls in the range `[pythFee, pythFee + totalKeeperFee)`, every update attempt passes the initial guard, makes the external Pyth call, then reverts at the keeper-fee check — causing price-feed updates to fail and keepers to waste gas.

---

### Finding Description

`updatePriceFeeds` executes two sequential fee deductions with two separate balance checks:

**Step 1 — Initial guard (line 295):**
```solidity
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```
This only validates `balance >= pythFee`.

**Step 2 — Deduct pythFee (line 305):**
```solidity
status.balanceInWei -= pythFee;
```
The balance is now `balance - pythFee`.

**Step 3 — Keeper-fee check inside `_processFeesAndPayKeeper` (line 852):**
```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```
This check operates on the **already-reduced** balance.

The initial guard at line 295 is therefore insufficient: it does not verify that the balance covers **both** fees. Any subscription whose balance satisfies `pythFee ≤ balance < pythFee + totalKeeperFee` will:

1. Pass the initial guard.
2. Have `pythFee` deducted from its tracked balance.
3. Trigger the external Pyth call (consuming gas and ETH).
4. Fail the keeper-fee check.
5. Revert — rolling back state changes, but the keeper has already spent gas.

This is the direct analog of the ZKsync `_verifyWithdrawalLimit` bug: a mutable balance value is used as the base for a limit check **after** it has already been reduced by a prior operation in the same call. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

- **Price-feed staleness (DoS):** Any subscription whose balance has drained into the range `[pythFee, pythFee + totalKeeperFee)` cannot be updated. Every keeper call reverts after the external Pyth call, leaving prices stale.
- **Keeper gas waste:** Keepers submit transactions that always revert, burning gas with no compensation.
- **Silent failure:** The initial check passes, so neither the keeper nor the subscription manager receives an early, clear signal that the balance is insufficient for the full operation.
- **`getMinimumBalance` does not prevent this:** `getMinimumBalance` returns `numPriceFeeds × minimumBalancePerFeed` — a static value set by the admin. Because `totalKeeperFee` is dynamic (gas-price-dependent), a subscription at exactly `getMinimumBalance` can still fall into the failing range when gas prices spike. [3](#0-2) [4](#0-3) 

---

### Likelihood Explanation

- **Natural occurrence:** As a subscription's balance drains through normal keeper payments, it will eventually enter the failing range without any attacker action.
- **Unprivileged trigger:** Any keeper (permissionless) calling `updatePriceFeeds` triggers the failure path. No special role is required.
- **Gas-price sensitivity:** On chains with volatile gas prices, `totalKeeperFee` can spike, widening the failing range and making the condition more likely.
- **Existing test acknowledges the gap:** `testUpdatePriceFeedsRevertsInsufficientBalanceForKeeperFee` explicitly mocks `getMinimumBalance` to `0` to expose this exact failure mode, confirming the developers are aware the two checks are independent. [5](#0-4) 

---

### Recommendation

Replace the partial pre-flight check with a combined check that validates the balance against the total expected cost before any state mutation or external call:

```solidity
// Estimate minimum keeper fee (static component only; gas component is a lower bound)
uint256 minKeeperFee = uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length
                     + GAS_OVERHEAD * tx.gasprice;

if (status.balanceInWei < pythFee + minKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This mirrors the fix recommended for the ZKsync bug: snapshot the relevant base value (total balance) before any deduction and use that snapshot for all limit calculations within the same call.

Additionally, `getMinimumBalance` should be calibrated to always exceed `pythFee + maxExpectedKeeperFee` so that subscriptions at minimum balance can always be updated.

---

### Proof of Concept

```
Setup:
  pythFee          = 1_000 wei  (returned by pyth.getUpdateFee)
  totalKeeperFee   = 5_000 wei  (gas cost + singleUpdateKeeperFeeInWei * numFeeds)
  subscription.balanceInWei = 4_999 wei

Call updatePriceFeeds():
  1. Line 295: 4_999 >= 1_000  → check PASSES
  2. Line 305: balanceInWei = 4_999 - 1_000 = 3_999
  3. External Pyth call made, gas consumed
  4. _processFeesAndPayKeeper: 3_999 < 5_000 → REVERT InsufficientBalance
  5. All state rolled back; keeper loses gas; price feeds NOT updated.

Expected (correct) behavior:
  Pre-flight check: 4_999 < 1_000 + 5_000 → revert EARLY, before external call.
```

The subscription balance of `4_999 wei` is above `pythFee` (passes the current guard) but below `pythFee + totalKeeperFee` (the true requirement). The current code allows the external call to proceed before discovering the shortfall, wasting keeper gas and leaving price feeds stale. [6](#0-5) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L734-738)
```text
    function getMinimumBalance(
        uint8 numPriceFeeds
    ) external view override returns (uint256 minimumBalanceInWei) {
        // TODO: Consider adding a base minimum balance independent of feed count
        return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
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

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L19-22)
```text
        /// Fee in wei charged to subscribers per single update triggered by a keeper
        uint128 singleUpdateKeeperFeeInWei;
        /// Minimum balance required per price feed in a subscription
        uint128 minimumBalancePerFeed;
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1373-1437)
```text
    function testUpdatePriceFeedsRevertsInsufficientBalanceForKeeperFee()
        public
    {
        // Set gas price
        uint256 gasPrice = 0.5 gwei;
        vm.txGasPrice(gasPrice);

        // Mock the minimum balance for the subscription to be
        // zero so that we can test the keeper fee
        vm.mockCall(
            address(scheduler),
            abi.encodeWithSelector(Scheduler.getMinimumBalance.selector),
            abi.encode(0)
        );

        // Add subscription
        uint256 subscriptionId = addTestSubscription(
            scheduler,
            address(reader)
        );
        bytes32[] memory priceIds = createPriceIds();

        // Prepare update data and get Pyth fee
        uint64 publishTime = SafeCast.toUint64(block.timestamp);
        PythStructs.PriceFeed[] memory priceFeeds;
        uint64[] memory slots;
        (priceFeeds, slots) = createMockPriceFeedsWithSlots(
            publishTime,
            priceIds.length
        );
        uint256 mockPythFee = MOCK_PYTH_FEE_PER_FEED * priceIds.length;
        mockParsePriceFeedUpdatesWithSlotsStrict(pyth, priceFeeds, slots);
        bytes[] memory updateData = createMockUpdateData(priceFeeds);

        // Calculate minimum keeper fee (overhead + feed-specific fee)
        // The real cost is more because of the gas used in the updatePriceFeeds function
        uint256 minKeeperFee = (scheduler.GAS_OVERHEAD() * gasPrice) +
            (uint256(scheduler.getSingleUpdateKeeperFeeInWei()) *
                priceIds.length);

        // Fund subscription without enough for Pyth fee + keeper fee
        // It won't be enough because of the gas cost of updatePriceFeeds
        uint256 fundAmount = mockPythFee + minKeeperFee;
        scheduler.addFunds{value: fundAmount}(subscriptionId);

        // Get and print the subscription balance before attempting the update
        (, SchedulerStructs.SubscriptionStatus memory status) = scheduler
            .getSubscription(subscriptionId);
        console.log(
            "Subscription balance before update:",
            vm.toString(status.balanceInWei)
        );
        console.log("Required Pyth fee:", vm.toString(mockPythFee));
        console.log("Minimum keeper fee:", vm.toString(minKeeperFee));
        console.log(
            "Total minimum required:",
            vm.toString(mockPythFee + minKeeperFee)
        );

        // Expect revert due to insufficient balance for total fee
        vm.expectRevert(
            abi.encodeWithSelector(SchedulerErrors.InsufficientBalance.selector)
        );
        vm.prank(pusher);
        scheduler.updatePriceFeeds(subscriptionId, updateData);
```
