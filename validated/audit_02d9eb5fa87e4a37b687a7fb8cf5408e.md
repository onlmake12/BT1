### Title
Static `minimumBalance` Insufficient to Cover Keeper Fees During Gas Price Spikes - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

The Scheduler contract's `minimumBalance` requirement is a fixed admin-set value that does not account for gas price volatility. The keeper fee is computed dynamically using `tx.gasprice` at execution time, meaning a subscription funded to exactly the required minimum can become unfulfillable during gas price spikes — the keeper's fee calculation will exceed the subscription balance and revert.

### Finding Description

In `Scheduler._processFeesAndPayKeeper`, the keeper reimbursement is calculated as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [1](#0-0) 

The `tx.gasprice` here is the keeper's actual gas price at execution time — a dynamic, network-dependent value. However, the minimum balance a subscriber must hold is:

```solidity
function getMinimumBalance(uint8 numPriceFeeds) external view override returns (uint256 minimumBalanceInWei) {
    return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
}
``` [2](#0-1) 

`minimumBalancePerFeed` is a fixed admin-set constant stored in state: [3](#0-2) 

It is set at initialization and updated only by admin governance: [4](#0-3) 

The `GAS_OVERHEAD` constant is `30000` gas: [5](#0-4) 

**The mismatch**: `minimumBalance` is gas-price-agnostic, but `totalKeeperFee` scales linearly with `tx.gasprice`. If `minimumBalancePerFeed` was calibrated at 10 gwei gas prices and gas spikes to 200 gwei, the keeper fee for a single update can be 20× larger than anticipated, exceeding the subscription's balance.

### Impact Explanation

When `totalKeeperFee > status.balanceInWei`, `updatePriceFeeds` reverts with `InsufficientBalance`. No keeper will call the function profitably. The subscription's price feeds stop being updated entirely, even though the subscriber funded it to the protocol-required minimum. Downstream protocols consuming these feeds via `getPricesNoOlderThan` or `getEmaPricesNoOlderThan` will receive stale prices or revert with `StalePrice`, potentially causing liquidation failures, mispriced trades, or protocol insolvency in integrating DeFi applications. [6](#0-5) 

### Likelihood Explanation

Gas price spikes are a regular occurrence on Ethereum mainnet (e.g., NFT mints, token launches, network congestion). A subscriber who creates a subscription at low gas prices and funds to exactly the minimum balance will have their subscription silently stop updating during any significant gas price spike. The subscriber has no on-chain signal that their subscription is unfulfillable — it remains marked `isActive = true`.

### Recommendation

1. Make `getMinimumBalance` gas-price-aware by incorporating a gas price estimate (e.g., `block.basefee`) into the minimum balance calculation, so the minimum always covers at least N keeper updates at current gas prices.
2. Alternatively, emit an event or set a flag when `updatePriceFeeds` fails due to insufficient balance, so subscribers can be notified off-chain.
3. Consider allowing keepers to partially drain a subscription (down to zero) rather than reverting, so at least the last possible update goes through.

### Proof of Concept

1. Admin deploys Scheduler with `minimumBalancePerFeed = 0.01 ETH` (calibrated at ~10 gwei gas).
2. Alice calls `createSubscription` with 2 price feeds, depositing exactly `minimumBalance = 0.02 ETH`.
3. Gas prices spike to 200 gwei. A keeper's `updatePriceFeeds` call uses ~200,000 gas.
4. Keeper fee = `(200,000 + 30,000) * 200e9 = 46,000,000,000,000,000 wei = 0.046 ETH`.
5. `0.046 ETH > 0.02 ETH` → `_processFeesAndPayKeeper` reverts with `InsufficientBalance`.
6. No keeper will call `updatePriceFeeds` for Alice's subscription. Her feeds go stale.
7. Any protocol calling `getPricesNoOlderThan` on Alice's subscription reverts with `StalePrice`. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L20-22)
```text
        uint128 singleUpdateKeeperFeeInWei;
        /// Minimum balance required per price feed in a subscription
        uint128 minimumBalancePerFeed;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L82-89)
```text
    function setMinimumBalancePerFeed(uint128 newMinimumBalance) external {
        _authorizeAdminAction();

        uint oldBalance = _state.minimumBalancePerFeed;
        _state.minimumBalancePerFeed = newMinimumBalance;

        emit MinimumBalancePerFeedSet(oldBalance, newMinimumBalance);
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L28-29)
```text
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```
