### Title
`GAS_OVERHEAD` Constant Underestimates Post-Checkpoint Gas, Causing Systematic Keeper Underpayment — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler._processFeesAndPayKeeper`, the `gasleft()` checkpoint is taken **before** the ETH transfer and two storage writes that complete the function. The `GAS_OVERHEAD = 30_000` constant is documented as covering "tx overhead for a keeper to call updatePriceFeeds," but it must also cover the post-checkpoint gas (SSTORE × 2 + ETH CALL). The actual post-checkpoint gas is ~20,000–35,000 gas, which alone exceeds what `GAS_OVERHEAD` can reasonably cover after also accounting for the 21,000-gas base transaction cost. Keepers are therefore systematically underpaid on every `updatePriceFeeds` call.

### Finding Description

`updatePriceFeeds` captures `startGas = gasleft()` at line 279, then calls `_processFeesAndPayKeeper(status, startGas, ...)` at line 345. Inside `_processFeesAndPayKeeper`, the fee is computed as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
``` [1](#0-0) 

The `gasleft()` call at line 846 is the **last gas checkpoint**. After it, the function still executes:

- `status.balanceInWei -= totalKeeperFee` — SSTORE (~5,000 warm / ~20,000 cold)
- `status.totalSpent += totalKeeperFee` — SSTORE (~5,000 warm / ~20,000 cold)
- `msg.sender.call{value: totalKeeperFee}("")` — ETH CALL (~9,000 gas for non-empty recipient) [2](#0-1) 

These post-checkpoint operations consume approximately **20,000–35,000 gas** that is never captured by `startGas - gasleft()`. `GAS_OVERHEAD` is set to `30_000`: [3](#0-2) 

`GAS_OVERHEAD` must simultaneously cover:
1. **Pre-function overhead**: 21,000 base tx gas + calldata decoding (variable, but easily 4,000–20,000 gas for typical Pyth update payloads)
2. **Post-checkpoint overhead**: ~20,000–35,000 gas (two SSTOREs + ETH CALL)

The combined overhead is approximately **45,000–76,000 gas**, but `GAS_OVERHEAD = 30,000`. The keeper is underpaid by **15,000–46,000 gas** on every update. The existing test explicitly acknowledges the gas accounting gap: [4](#0-3) 

The test confirms `totalFeeDeducted > GAS_OVERHEAD * gasPrice + keeperFee + pythFee`, but this is because `startGas - gasleft()` captures in-function gas. The post-checkpoint gas remains uncompensated.

### Impact Explanation

Every keeper call to `updatePriceFeeds` results in the keeper paying more gas than they receive in compensation. At 10 gwei gas price and a 30,000-gas shortfall, each update costs the keeper ~0.0003 ETH out-of-pocket. Rational keepers operating at scale will eventually stop submitting updates when the cumulative loss exceeds their incentive, causing price feeds to go stale. Subscribers cannot prevent this — the underpayment is baked into the constant.

### Likelihood Explanation

The underpayment occurs on **every single call** to `updatePriceFeeds`. It is not conditional on any edge case. The effect is amplified at high gas prices (e.g., during network congestion), precisely when timely price updates are most critical.

### Recommendation

Move the `gasleft()` checkpoint to **after** the storage writes and ETH transfer, or split the accounting into two checkpoints:

```solidity
// After the ETH transfer:
uint256 gasAfterTransfer = gasleft();
uint256 gasCost = (startGas - gasAfterTransfer + GAS_OVERHEAD) * tx.gasprice;
```

Alternatively, increase `GAS_OVERHEAD` to at least `60_000` to cover both pre-function overhead (~21,000 base + calldata) and post-checkpoint overhead (~35,000 for two SSTOREs + ETH CALL), and benchmark the actual value with the gas benchmark test suite already present at: [5](#0-4) 

### Proof of Concept

1. Deploy `SchedulerUpgradeable` with `gasPrice = 10 gwei`.
2. Create a subscription with 1 price feed and fund it with the minimum balance.
3. Call `updatePriceFeeds` as a keeper EOA.
4. Measure: `keeper_gas_paid = gas_used_by_tx * gasPrice`. Measure: `keeper_fee_received = pusher.balance_after - pusher.balance_before`.
5. Observe `keeper_gas_paid > keeper_fee_received` by approximately `(post_checkpoint_gas - remaining_GAS_OVERHEAD_budget) * gasPrice`.

The existing test `testUpdatePriceFeedsRevertsInsufficientBalanceForKeeperFee` already funds the subscription with exactly `mockPythFee + GAS_OVERHEAD * gasPrice + singleUpdateKeeperFeeInWei * numFeeds` and expects a revert — confirming the protocol itself treats `GAS_OVERHEAD` as the sole overhead budget, while the actual overhead is higher. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L846-846)
```text
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L856-863)
```text
        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1344-1353)
```text
        // The real cost is more because of the gas used in the updatePriceFeeds function
        uint256 minKeeperFee = (scheduler.GAS_OVERHEAD() * gasPrice) +
            (uint256(scheduler.getSingleUpdateKeeperFeeInWei()) *
                params.priceIds.length);

        assertGt(
            totalFeeDeducted,
            minKeeperFee + mockPythFee,
            "Total fee deducted should be greater than the sum of keeper fee and Pyth fee (since gas usage of updatePriceFeeds is not accounted for)"
        );
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1407-1437)
```text
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

**File:** target_chains/ethereum/contracts/test/PulseSchedulerGasBenchmark.t.sol (L52-105)
```text
    // Helper function to run the price feed update benchmark with a specified number of feeds
    function _runUpdateAndQueryPriceFeedsBenchmark(uint8 numFeeds) internal {
        // Setup: Create subscription and perform initial update
        vm.prank(manager);
        uint256 subscriptionId = _setupSubscriptionWithInitialUpdate(numFeeds);
        (SchedulerStructs.SubscriptionParams memory params, ) = scheduler
            .getSubscription(subscriptionId);

        // Advance time to meet heartbeat criteria
        vm.warp(block.timestamp + 100);

        // Create new price feed updates with updated timestamp
        uint64 newPublishTime = SafeCast.toUint64(block.timestamp);
        PythStructs.PriceFeed[] memory newPriceFeeds;
        uint64[] memory newSlots;

        (newPriceFeeds, newSlots) = createMockPriceFeedsWithSlots(
            newPublishTime,
            numFeeds
        );

        // Mock Pyth response for the benchmark
        mockParsePriceFeedUpdatesWithSlotsStrict(pyth, newPriceFeeds, newSlots);

        // Actual benchmark: Measure gas for updating price feeds
        uint256 startGas = gasleft();
        scheduler.updatePriceFeeds(
            subscriptionId,
            createMockUpdateData(newPriceFeeds)
        );
        uint256 updateGasUsed = startGas - gasleft();

        console.log(
            "Gas used for updating %s feeds: %s",
            vm.toString(numFeeds),
            vm.toString(updateGasUsed)
        );

        // Benchmark querying the price feeds after updating
        uint256 queryStartGas = gasleft();
        scheduler.getPricesUnsafe(subscriptionId, params.priceIds);
        uint256 queryGasUsed = queryStartGas - gasleft();

        console.log(
            "Gas used for querying %s feeds: %s",
            vm.toString(numFeeds),
            vm.toString(queryGasUsed)
        );
        console.log(
            "Total gas used for updating and querying %s feeds: %s",
            vm.toString(numFeeds),
            vm.toString(updateGasUsed + queryGasUsed)
        );
    }
```
