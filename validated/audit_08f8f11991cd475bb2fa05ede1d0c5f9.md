### Title
Inactive Subscription Update Bypasses Validation, Enabling Permanent Fund Lock - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
In `Scheduler.sol`, the `updateSubscription` function contains a fast-path for the case where a subscription is inactive and will remain inactive (`!wasActive && !willBeActive`). This path skips `_validateSubscriptionParams` entirely and writes `newParams` directly to storage. Because `isPermanent` is a field inside `newParams`, a subscription manager can set `isPermanent = true` on an inactive subscription without going through the normal active-subscription flow. Once stored, both `withdrawFunds` and any future `updateSubscription` call are permanently blocked for that subscription, locking all deposited funds with no recovery path.

### Finding Description

`updateSubscription` in `Scheduler.sol` checks `currentParams.isPermanent` first, then immediately returns early if the subscription is inactive and will stay inactive:

```solidity
// Updates to permanent subscriptions are not allowed
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}

bool wasActive = currentParams.isActive;
bool willBeActive = newParams.isActive;
if (!wasActive && !willBeActive) {
    // Update subscription parameters
    _state.subscriptionParams[subscriptionId] = newParams;   // ← isPermanent written here
    emit SubscriptionUpdated(subscriptionId);
    return;
}
_validateSubscriptionParams(newParams);   // ← never reached
``` [1](#0-0) 

Because `_validateSubscriptionParams` is never called in this branch, the caller can supply any `newParams` struct, including one with `isPermanent = true`. The assignment `_state.subscriptionParams[subscriptionId] = newParams` then persists the permanent flag.

`withdrawFunds` explicitly blocks withdrawals from permanent subscriptions:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [2](#0-1) 

Any subsequent call to `updateSubscription` also hits the `isPermanent` guard at the top of the function and reverts. There is no admin override or emergency withdrawal path. The subscription balance is permanently locked in the contract.

### Impact Explanation

A subscription manager who (accidentally or through a buggy integration contract) calls `updateSubscription` on an inactive subscription with `newParams.isPermanent = true` will permanently lose all ETH held in that subscription's balance. There is no recovery mechanism: `withdrawFunds` reverts, `updateSubscription` reverts, and no admin function exists to rescue the funds. The impact is **permanent, irrecoverable loss of deposited ETH** for the subscription manager.

### Likelihood Explanation

The subscription manager is an unprivileged address — any user who created a subscription. The triggering sequence is:

1. Create a subscription (active).
2. Deactivate it via `updateSubscription` with `isActive = false`.
3. Call `updateSubscription` again with `isActive = false` and `isPermanent = true`.

Step 3 is a single transaction. A smart-contract wallet or DeFi integration that exposes `updateSubscription` with user-supplied parameters is directly exploitable by any caller. Even without a malicious actor, a developer mistake (copying params and toggling `isPermanent`) triggers the same outcome. The path is short, requires no special privilege, and the inconsistency is non-obvious because the `isPermanent` guard at the top of the function creates a false sense of safety.

### Recommendation

Move `_validateSubscriptionParams` to execute unconditionally before the `!wasActive && !willBeActive` early-return, or add an explicit check inside that branch that rejects any attempt to set `isPermanent = true` on a subscription that is not currently active:

```solidity
if (!wasActive && !willBeActive) {
    // Disallow escalating to permanent while inactive
    if (newParams.isPermanent && !currentParams.isPermanent) {
        revert SchedulerErrors.CannotUpdatePermanentSubscription();
    }
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;
}
```

Alternatively, require that `isPermanent` can only be set to `true` when the subscription is active, enforced inside `_validateSubscriptionParams`.

### Proof of Concept

```solidity
// 1. Manager creates a subscription (active, non-permanent)
uint256 subId = scheduler.createSubscription{value: 1 ether}(params);

// 2. Manager deactivates it
params.isActive = false;
scheduler.updateSubscription(subId, params);

// 3. Manager (or attacker with manager role) sets isPermanent = true
//    while subscription remains inactive — bypasses _validateSubscriptionParams
params.isPermanent = true;
scheduler.updateSubscription(subId, params);
// ↑ hits the !wasActive && !willBeActive fast-path, writes isPermanent = true

// 4. Funds are now permanently locked
scheduler.withdrawFunds(subId, 1 ether);
// ↑ reverts: CannotUpdatePermanentSubscription

scheduler.updateSubscription(subId, params);
// ↑ reverts: CannotUpdatePermanentSubscription (isPermanent guard at top)
``` [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L77-153)
```text
    function updateSubscription(
        uint256 subscriptionId,
        SchedulerStructs.SubscriptionParams memory newParams
    ) external payable override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage currentStatus = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage currentParams = _state
            .subscriptionParams[subscriptionId];

        // Add incoming funds to balance
        currentStatus.balanceInWei += msg.value;

        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }

        // If subscription is inactive and will remain inactive, no need to validate parameters
        bool wasActive = currentParams.isActive;
        bool willBeActive = newParams.isActive;
        if (!wasActive && !willBeActive) {
            // Update subscription parameters
            _state.subscriptionParams[subscriptionId] = newParams;
            emit SubscriptionUpdated(subscriptionId);
            return;
        }
        _validateSubscriptionParams(newParams);

        // Check minimum balance if subscription remains active
        if (willBeActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(newParams.priceIds.length)
            );
            if (currentStatus.balanceInWei < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }

        // Handle activation/deactivation
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
        } else if (wasActive && !willBeActive) {
            // Deactivating a subscription
            currentParams.isActive = false;
            _removeFromActiveSubscriptions(subscriptionId);
            emit SubscriptionDeactivated(subscriptionId);
        }

        // Clear price updates for removed price IDs before updating params
        bool newPriceIdsAdded = _clearRemovedPriceUpdates(
            subscriptionId,
            currentParams.priceIds,
            newParams.priceIds
        );

        // Reset priceLastUpdatedAt to 0 if new price IDs were added
        if (newPriceIdsAdded) {
            _state.subscriptionStatuses[subscriptionId].priceLastUpdatedAt = 0;
        }

        // Update subscription parameters
        _state.subscriptionParams[subscriptionId] = newParams;

        emit SubscriptionUpdated(subscriptionId);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-662)
```text
    function withdrawFunds(
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
