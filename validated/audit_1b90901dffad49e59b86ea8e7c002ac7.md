### Title
`updateSubscription` Skips Parameter Validation for Inactive-to-Inactive Transitions, Enabling Permanent Fund Lock — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `updateSubscription` function contains a validation bypass path that is absent in `createSubscription`. When both the current and new `isActive` states are `false`, the function stores caller-supplied `SubscriptionParams` directly without calling `_validateSubscriptionParams`. This allows the subscription manager to set `isPermanent = true` on an inactive subscription, permanently locking all deposited funds with no recovery path.

---

### Finding Description

`createSubscription` enforces two invariants before storing parameters:

1. It always overrides `subscriptionParams.isActive = true` (line 53), preventing creation of inactive subscriptions.
2. It always calls `_validateSubscriptionParams(subscriptionParams)` (line 35) before storing. [1](#0-0) 

`updateSubscription` has a bypass path at lines 97–102 that skips `_validateSubscriptionParams` entirely when both `wasActive` and `willBeActive` are `false`:

```solidity
if (!wasActive && !willBeActive) {
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;
}
``` [2](#0-1) 

The `newParams` struct is stored verbatim. The `isPermanent` field is part of `SubscriptionParams` and is not guarded in this path. Once `isPermanent = true` is written to storage on an inactive subscription, two irreversible consequences follow:

- `updateSubscription` checks `currentParams.isPermanent` at line 90 and reverts with `CannotUpdatePermanentSubscription` before any other logic runs — the subscription can never be reactivated.
- `withdrawFunds` checks `params.isPermanent` and reverts with the same error — funds can never be withdrawn. [3](#0-2) [4](#0-3) 

The bypass path also skips validation of `updateCriteria` (both flags false), empty `priceIds`, and duplicate `priceIds`, but the `isPermanent` path is the only one with an irreversible financial consequence.

---

### Impact Explanation

A subscription manager who calls `updateSubscription({isActive: false, isPermanent: true, ...})` on an already-inactive subscription triggers the bypass path. The subscription transitions to a state that is simultaneously inactive (cannot serve price updates) and permanent (cannot be updated, activated, or drained). All ETH held in `subscriptionStatuses[subscriptionId].balanceInWei` is permanently inaccessible. There is no admin override or escape hatch in the contract.

---

### Likelihood Explanation

The entry path is reachable by any unprivileged user who has previously created a subscription (i.e., any `subscriptionManager`). The trigger requires only two sequential calls: deactivate the subscription, then call `updateSubscription` with `isPermanent = true` and `isActive = false`. This can occur:

- Accidentally in a contract that manages subscriptions on behalf of users (e.g., a vault or DAO wrapper) if it passes through user-supplied `SubscriptionParams` without sanitizing `isPermanent`.
- Deliberately by a malicious governance proposal targeting a contract-owned subscription.

The `onlyManager` guard does not prevent the manager themselves from triggering this — it only prevents third parties.

---

### Recommendation

1. **Short term**: In the inactive-to-inactive bypass path, explicitly reject any attempt to set `isPermanent = true` if the subscription is not already permanent:
   ```solidity
   if (!wasActive && !willBeActive) {
       require(
           !newParams.isPermanent || currentParams.isPermanent,
           "Cannot make inactive subscription permanent"
       );
       _state.subscriptionParams[subscriptionId] = newParams;
       emit SubscriptionUpdated(subscriptionId);
       return;
   }
   ```
2. **Long term**: Document the invariant that `isPermanent` is a one-way flag and add a dedicated `makeSubscriptionPermanent` function with explicit deposit-limit and balance checks, removing the flag from the general `SubscriptionParams` update path.
3. Create unit tests for the inactive-to-inactive bypass path covering all fields that differ from `createSubscription`'s validated path.

---

### Proof of Concept

```solidity
// 1. Manager creates a subscription (isActive forced to true, isPermanent = false)
uint256 subId = scheduler.createSubscription{value: 10 ether}(params);

// 2. Manager deactivates the subscription
params.isActive = false;
scheduler.updateSubscription(subId, params);
// currentParams.isActive == false

// 3. Manager calls updateSubscription with isPermanent = true, isActive = false
//    wasActive = false, willBeActive = false → bypass path triggered
params.isPermanent = true;
scheduler.updateSubscription(subId, params);
// _state.subscriptionParams[subId].isPermanent == true, isActive == false

// 4. Funds are permanently locked
scheduler.withdrawFunds(subId, 10 ether);
// ↑ reverts: CannotUpdatePermanentSubscription

scheduler.updateSubscription(subId, activeParams);
// ↑ reverts: CannotUpdatePermanentSubscription (checked before isActive logic)
``` [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L32-75)
```text
    function createSubscription(
        SchedulerStructs.SubscriptionParams memory subscriptionParams
    ) external payable override returns (uint256 subscriptionId) {
        _validateSubscriptionParams(subscriptionParams);

        // Calculate minimum balance required for this subscription
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );

        // Ensure enough funds were provided
        if (msg.value < minimumBalance) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        // Set subscription to active
        subscriptionParams.isActive = true;

        subscriptionId = _state.subscriptionNumber++;

        // Store the subscription parameters
        _state.subscriptionParams[subscriptionId] = subscriptionParams;

        // Initialize subscription status
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        status.priceLastUpdatedAt = 0;
        status.balanceInWei = msg.value;
        status.totalUpdates = 0;
        status.totalSpent = 0;

        // Map subscription ID to manager
        _state.subscriptionManager[subscriptionId] = msg.sender;

        _addToActiveSubscriptions(subscriptionId);

        emit SubscriptionCreated(subscriptionId, msg.sender);
        return subscriptionId;
    }
```

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```
