### Title
Unconditional Zero-Value ETH Transfer in `_processFeesAndPayKeeper` Blocks Keeper Contracts Without `fallback`/`receive` - (`Scheduler.sol`)

---

### Summary

`Scheduler._processFeesAndPayKeeper()` unconditionally executes `msg.sender.call{value: totalKeeperFee}("")` even when `totalKeeperFee` evaluates to zero. Any keeper that is a smart contract without a `fallback` or `receive` function will have every `updatePriceFeeds()` call revert at the payment step, permanently preventing that keeper from pushing price updates to any subscription.

---

### Finding Description

In `Scheduler._processFeesAndPayKeeper()`, the keeper payment is issued unconditionally:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
if (!sent) {
    revert SchedulerErrors.KeeperPaymentFailed();
}
``` [1](#0-0) 

`totalKeeperFee` is zero when both of the following hold:
- `tx.gasprice == 0` — valid on several EVM-compatible L2s and testnets
- `_state.singleUpdateKeeperFeeInWei == 0` — a legitimate admin configuration

When `totalKeeperFee == 0`, the low-level call `msg.sender.call{value: 0}("")` is issued. The EVM forwards this as a plain ETH transfer with empty calldata. If `msg.sender` is a contract that implements neither `fallback` nor `receive`, the call returns `false`, `sent` is `false`, and the function reverts with `KeeperPaymentFailed`.

Even when `totalKeeperFee > 0`, any keeper contract without `fallback`/`receive` will fail to receive the payment, causing the same revert. The zero-value case is the more surprising variant: the keeper would not expect a zero-ETH transfer to be attempted at all.

The revert unwinds the entire `updatePriceFeeds()` call: [2](#0-1) 

All intermediate state changes (Pyth fee deduction, price feed storage, `priceLastUpdatedAt` update) are rolled back. No price data is committed and no keeper fee is paid.

---

### Impact Explanation

A keeper implemented as a smart contract without `fallback`/`receive` — a common pattern for automation bots, multisigs, and protocol-owned keepers — is permanently unable to call `updatePriceFeeds()`. Every attempt reverts at `_processFeesAndPayKeeper`. Subscriptions that depend on such a keeper receive no price updates, causing them to go stale indefinitely. The subscription balance is not drained (the revert rolls back the deduction), but the service the subscription paid for is never rendered. [3](#0-2) 

---

### Likelihood Explanation

Automated keeper bots are frequently deployed as contracts (e.g., Gelato resolvers, Chainlink Automation-compatible contracts, custom executor contracts). Many such contracts do not implement `fallback`/`receive` because they are not designed to hold ETH. The zero-value path is reachable on any chain where `tx.gasprice` can be zero and `singleUpdateKeeperFeeInWei` is configured as zero by the admin. Both conditions are realistic in production deployments on L2s. [4](#0-3) 

---

### Recommendation

Guard the ETH transfer with a zero-value check, mirroring the fix applied to the analogous Taiko bug:

```solidity
if (totalKeeperFee > 0) {
    (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
    if (!sent) {
        revert SchedulerErrors.KeeperPaymentFailed();
    }
}
```

This eliminates the spurious zero-value call while preserving all existing fee-accounting logic. [5](#0-4) 

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

// Keeper contract with no fallback/receive
contract NoReceiveKeeper {
    IScheduler scheduler;

    constructor(address _scheduler) {
        scheduler = IScheduler(_scheduler);
    }

    function triggerUpdate(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external {
        // This will always revert with KeeperPaymentFailed when
        // totalKeeperFee == 0 (tx.gasprice == 0 && singleUpdateKeeperFeeInWei == 0)
        // OR when totalKeeperFee > 0 (no receive/fallback to accept ETH)
        scheduler.updatePriceFeeds(subscriptionId, updateData);
    }
    // No fallback() or receive() defined
}
```

Deploy `NoReceiveKeeper`, fund a subscription, configure `singleUpdateKeeperFeeInWei = 0`, submit the transaction with `gasPrice = 0`. Every call to `triggerUpdate` reverts at `KeeperPaymentFailed` despite valid price data being provided. [6](#0-5)

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
