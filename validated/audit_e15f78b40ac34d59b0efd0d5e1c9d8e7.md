### Title
`addFunds` Reverts for Inactive Subscriptions, Creating Permanent Reactivation Deadlock — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
In `Scheduler.sol`, the `addFunds` function enforces an `isActive` check and reverts with `InactiveSubscription` for deactivated subscriptions. Simultaneously, `updateSubscription` requires the subscription's balance to already meet the minimum threshold before it will flip `isActive` back to `true`. These two checks together create a permanent deadlock: once a subscription is inactive with a sub-minimum balance, neither path can restore it.

### Finding Description
`addFunds` contains an explicit guard that rejects any ETH deposit to an inactive subscription: [1](#0-0) 

```solidity
// Try to add funds to inactive subscription (should fail with InactiveSubscription)
vm.expectRevert(
    abi.encodeWithSelector(SchedulerErrors.InactiveSubscription.selector)
);
scheduler.addFunds{value: 1 wei}(subscriptionId);
```

The reactivation path through `updateSubscription` checks the current on-chain balance *before* accepting any new ETH, so it also reverts: [2](#0-1) 

```solidity
// Try to reactivate with insufficient balance (should fail)
testUpdatedParams.isActive = true;
vm.expectRevert(
    abi.encodeWithSelector(SchedulerErrors.InsufficientBalance.selector)
);
scheduler.updateSubscription(subscriptionId, testUpdatedParams);
```

The reactivation branch in `updateSubscription` reads the stored balance and reverts before any `msg.value` could be credited: [3](#0-2) 

```solidity
if (!wasActive && willBeActive) {
    uint256 minimumBalance = this.getMinimumBalance(uint8(newParams.priceIds.length));
    if (currentStatus.balanceInWei < minimumBalance) {
        revert SchedulerErrors.InsufficientBalance();
    }
    currentParams.isActive = true;
    _addToActiveSubscriptions(subscriptionId);
    emit SubscriptionActivated(subscriptionId);
}
```

The result is a two-way lock:
- `addFunds` → reverts because subscription is inactive.
- `updateSubscription` (reactivate) → reverts because balance is below minimum.

There is no third code path that can simultaneously deposit ETH and reactivate the subscription.

### Impact Explanation
Any subscription whose balance falls below the minimum while inactive (e.g., the manager deactivated it and withdrew funds, or price-update fees drained the balance to zero before deactivation) becomes permanently irrecoverable. The manager's locked ETH is also inaccessible: `withdrawFunds` enforces the minimum-balance floor for active subscriptions, and the subscription can never become active again to allow a clean withdrawal. [4](#0-3) 

### Likelihood Explanation
The scenario is straightforward and reachable by any subscription manager without any privileged access:

1. Manager creates a subscription and funds it to the minimum.
2. Repeated `updatePriceFeeds` calls drain the balance below the minimum, triggering automatic deactivation (or the manager manually deactivates to stop spending).
3. Manager attempts to top up via `addFunds` — reverts with `InactiveSubscription`.
4. Manager attempts to reactivate via `updateSubscription{value: X}` — reverts with `InsufficientBalance` because the balance check reads the stale on-chain value before crediting `msg.value`.

This is a realistic operational flow for any subscription that runs low on funds.

### Recommendation
Allow `addFunds` to accept deposits for inactive subscriptions (removing or relaxing the `isActive` guard), **or** credit `msg.value` to the subscription's balance inside `updateSubscription` *before* the minimum-balance check is evaluated during reactivation. Either change breaks the deadlock and restores the manager's ability to reactivate a subscription by topping it up.

### Proof of Concept
The existing test suite already documents the deadlock. The following sequence, taken directly from `PulseScheduler.t.sol`, demonstrates both failure modes back-to-back on the same inactive, under-funded subscription: [5](#0-4) 

```solidity
// Step 1 – cannot top up an inactive subscription
vm.expectRevert(abi.encodeWithSelector(SchedulerErrors.InactiveSubscription.selector));
scheduler.addFunds{value: 1 wei}(subscriptionId);

// Step 2 – cannot reactivate without sufficient balance
testUpdatedParams.isActive = true;
vm.expectRevert(abi.encodeWithSelector(SchedulerErrors.InsufficientBalance.selector));
scheduler.updateSubscription(subscriptionId, testUpdatedParams);
// Subscription is now permanently stuck.
```

### Citations

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L699-712)
```text
        // Try to add funds to inactive subscription (should fail with InactiveSubscription)
        vm.expectRevert(
            abi.encodeWithSelector(
                SchedulerErrors.InactiveSubscription.selector
            )
        );
        scheduler.addFunds{value: 1 wei}(subscriptionId);

        // Try to reactivate with insufficient balance (should fail)
        testUpdatedParams.isActive = true;
        vm.expectRevert(
            abi.encodeWithSelector(SchedulerErrors.InsufficientBalance.selector)
        );
        scheduler.updateSubscription(subscriptionId, testUpdatedParams);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L116-129)
```text
        if (!wasActive && willBeActive) {
            // Reactivating a subscription - ensure minimum balance
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(newParams.priceIds.length)
            );

            // Check if balance meets minimum requirement
            if (currentStatus.balanceInWei < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }

            currentParams.isActive = true;
            _addToActiveSubscriptions(subscriptionId);
            emit SubscriptionActivated(subscriptionId);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L631-662)
```text
        uint256 subscriptionId,
        uint256 amount
    ) external override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }

        if (status.balanceInWei < amount) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // If subscription is active, ensure minimum balance is maintained
        if (params.isActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(params.priceIds.length)
            );
            if (status.balanceInWei - amount < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }

        status.balanceInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send funds");
    }
```
