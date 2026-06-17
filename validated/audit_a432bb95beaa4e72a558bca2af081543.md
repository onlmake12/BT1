### Title
Inactive-to-Inactive `updateSubscription` Path Skips All Parameter Validation, Allowing Manager to Permanently Lock Funds via Unguarded `isPermanent` Flag - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, `updateSubscription` contains an early-return shortcut when both the current and new subscription states are inactive. This path stores `newParams` directly without calling `_validateSubscriptionParams` and without enforcing the `MAX_DEPOSIT_LIMIT` constraint that normally applies to permanent subscriptions. A manager can exploit this to set `isPermanent = true` on an inactive subscription that holds a balance exceeding `MAX_DEPOSIT_LIMIT`, permanently and irrecoverably locking those funds in the contract.

---

### Finding Description

`updateSubscription` checks `currentParams.isPermanent` at line 90 to block updates to already-permanent subscriptions. Immediately after, at lines 95–102, it reads `wasActive` and `willBeActive` and, when both are `false`, executes an early return that writes `newParams` directly to storage and returns:

```solidity
if (!wasActive && !willBeActive) {
    // Update subscription parameters
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;                          // ← skips _validateSubscriptionParams
}
``` [1](#0-0) 

This early return bypasses `_validateSubscriptionParams(newParams)` entirely. Because `newParams` is a caller-supplied struct that includes the `isPermanent` field, the manager can supply `newParams.isPermanent = true` and have it written to storage with no deposit-limit check and no parameter validation.

The deposit-limit guard for permanent subscriptions exists only in `createSubscription` (line 48) and `addFunds` (line 613):

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [2](#0-1) 

Neither guard is reached through the inactive-to-inactive path.

---

### Impact Explanation

Once `isPermanent = true` is written to an inactive subscription:

- `updateSubscription` reverts at line 90–92 (`CannotUpdatePermanentSubscription`) for any future call, so the subscription can never be reactivated.
- `withdrawFunds` reverts at line 640–642 for the same reason, so the balance is irrecoverable. [3](#0-2) 

The manager's ETH is permanently locked in the contract beyond the protocol's intended `MAX_DEPOSIT_LIMIT`. Because non-permanent subscriptions have no deposit cap, a manager can first deposit an arbitrarily large balance, then convert to permanent via this path, locking far more ETH than the protocol intends to hold in permanent subscriptions.

**Impact: permanent, irrecoverable loss of manager funds; protocol invariant (deposit cap for permanent subscriptions) broken.**

---

### Likelihood Explanation

The attack requires only the manager's own address — no privileged key, no governance majority, no external oracle. The three-step sequence (create → deactivate → set permanent via inactive path) is reachable by any subscription manager. A manager may trigger this accidentally (e.g., copying params and toggling `isPermanent` while the subscription is inactive) or intentionally to grief themselves or to test the protocol boundary.

**Likelihood: Medium** — reachable by any subscription manager with no special preconditions beyond owning a subscription.

---

### Recommendation

In the inactive-to-inactive branch, either:

1. **Reject `isPermanent` escalation**: add a check before the early return:
   ```solidity
   if (!currentParams.isPermanent && newParams.isPermanent) {
       revert SchedulerErrors.CannotSetPermanentWhileInactive();
   }
   ```
2. **Or apply the deposit-limit guard** when `newParams.isPermanent == true` regardless of the active/inactive path, mirroring the check in `createSubscription`.

Additionally, consider whether the inactive-to-inactive path should call `_validateSubscriptionParams(newParams)` to prevent storing structurally invalid parameters (empty `priceIds`, invalid `updateCriteria`) that would permanently block reactivation.

---

### Proof of Concept

```solidity
// 1. Create a non-permanent subscription with balance > MAX_DEPOSIT_LIMIT
//    (no cap applies to non-permanent subscriptions)
uint256 largeDeposit = scheduler.MAX_DEPOSIT_LIMIT() + 10 ether;
vm.deal(attacker, largeDeposit);
vm.prank(attacker);
uint256 subId = scheduler.createSubscription{value: largeDeposit}(params);
// params.isPermanent = false, params.isActive = true (forced by createSubscription)

// 2. Deactivate the subscription (active → inactive)
params.isActive = false;
vm.prank(attacker);
scheduler.updateSubscription(subId, params);

// 3. Set isPermanent = true via the inactive-to-inactive path
//    _validateSubscriptionParams is skipped; no deposit-limit check
params.isPermanent = true;
vm.prank(attacker);
scheduler.updateSubscription(subId, params);

// 4. Verify: subscription is now permanent with balance > MAX_DEPOSIT_LIMIT
(SchedulerStructs.SubscriptionParams memory stored,
 SchedulerStructs.SubscriptionStatus memory status) = scheduler.getSubscription(subId);
assert(stored.isPermanent == true);
assert(status.balanceInWei > scheduler.MAX_DEPOSIT_LIMIT());

// 5. Verify: funds are permanently locked — withdrawFunds reverts
vm.prank(attacker);
vm.expectRevert(SchedulerErrors.CannotUpdatePermanentSubscription.selector);
scheduler.withdrawFunds(subId, 1 ether);
``` [4](#0-3) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L77-102)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-642)
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
```
