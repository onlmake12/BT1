### Title
Scheduler: `isPermanent` flag is irreversible with no admin override, permanently locking subscription funds — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `isPermanent` flag in `Scheduler.sol` can be set to `true` by any subscription manager but can never be unset. Once set, the subscription manager permanently loses the ability to withdraw deposited ETH, update parameters, or deactivate the subscription. There is no admin override, no governance escape hatch, and no recovery path.

### Finding Description

In `Scheduler.sol`, `updateSubscription` immediately reverts for any call on a permanent subscription: [1](#0-0) 

```solidity
// Updates to permanent subscriptions are not allowed
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
```

`withdrawFunds` is similarly blocked: [2](#0-1) 

```solidity
// Prevent withdrawals from permanent subscriptions
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
```

The flag is set by the subscription manager via `updateSubscription` or at creation time via `createSubscription`. Once set, the check at line 90 blocks every subsequent `updateSubscription` call — including any attempt to set `isPermanent = false`. The test suite explicitly confirms this is one-directional: [3](#0-2) 

There is no `disablePermanent()`, no admin bypass in `SchedulerGovernance`, and no governance instruction that can override this state. [4](#0-3) 

Additionally, `addFunds` is callable by **anyone** (not just the manager) on a permanent subscription: [5](#0-4) 

This means any third party can permanently lock additional ETH into a permanent subscription that the manager can never recover.

### Impact Explanation

- **Permanent fund lock**: The subscription manager can never withdraw any portion of their deposited ETH from a permanent subscription. When the subscription balance is depleted by price-update fees, any residual balance is irrecoverable.
- **No parameter correction**: If the subscription was created with incorrect `priceIds`, `updateCriteria`, or `readerWhitelist`, there is no way to fix it.
- **Third-party ETH lock**: Any address can call `addFunds` on a permanent subscription, permanently locking their own ETH with no recovery path.
- **Impact: Medium** — direct, permanent loss of deposited ETH for the subscription manager and any third party who calls `addFunds`.

### Likelihood Explanation

- Any subscription manager can trigger this by setting `isPermanent = true` — no privileged role required.
- The flag can be set at creation time or via `updateSubscription`, with no confirmation step or warning.
- Subscription managers integrating programmatically may set this flag without fully understanding its irreversibility.
- **Likelihood: Medium** — the action is explicit but the permanent consequences (especially the fund-lock) are not surfaced with any on-chain warning.

### Recommendation

Add an admin-level override that allows the Scheduler admin (or governance) to unset `isPermanent` in emergency situations, analogous to the `whitelistEnabled` toggle suggested in the external report:

```solidity
// In SchedulerGovernance.sol
function disablePermanentSubscription(uint256 subscriptionId) external {
    _authorizeAdminAction();
    _state.subscriptionParams[subscriptionId].isPermanent = false;
    emit SubscriptionPermanentDisabled(subscriptionId);
}
```

Alternatively, restrict `addFunds` to the subscription manager only for permanent subscriptions, to prevent third-party ETH from being permanently locked.

### Proof of Concept

1. Subscription manager calls `createSubscription` with `isPermanent = true` and deposits 1 ETH.
2. Manager later calls `withdrawFunds(subscriptionId, 0.5 ether)` → reverts with `CannotUpdatePermanentSubscription`.
3. Manager calls `updateSubscription(subscriptionId, params_with_isPermanent_false)` → reverts with `CannotUpdatePermanentSubscription`.
4. Third party calls `addFunds{value: 0.1 ether}(subscriptionId)` → succeeds, permanently locking an additional 0.1 ETH.
5. No admin function exists to recover any of these funds. [6](#0-5) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-617)
```text
    function addFunds(uint256 subscriptionId) external payable override {
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Check deposit limit for permanent subscriptions
        if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        status.balanceInWei += msg.value;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-660)
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
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L870-879)
```text
        // Test 1: Cannot disable isPermanent flag
        SchedulerStructs.SubscriptionParams memory updatedParams = storedParams;
        updatedParams.isPermanent = false;

        vm.expectRevert(
            abi.encodeWithSelector(
                SchedulerErrors.CannotUpdatePermanentSubscription.selector
            )
        );
        scheduler.updateSubscription(subscriptionId, updatedParams);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L1-60)
```text
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "./SchedulerState.sol";
import "@pythnetwork/pulse-sdk-solidity/SchedulerErrors.sol";

/**
 * @dev `SchedulerGovernance` defines governance capabilities for the Pulse contract.
 */
abstract contract SchedulerGovernance is SchedulerState {
    event NewAdminProposed(address oldAdmin, address newAdmin);
    event NewAdminAccepted(address oldAdmin, address newAdmin);
    event SingleUpdateKeeperFeeSet(uint oldFee, uint newFee);
    event MinimumBalancePerFeedSet(uint oldBalance, uint newBalance);

    /**
     * @dev Returns the address of the proposed admin.
     */
    function proposedAdmin() public view virtual returns (address) {
        return _state.proposedAdmin;
    }

    /**
     * @dev Returns the address of the current admin.
     */
    function getAdmin() external view returns (address) {
        return _state.admin;
    }

    /**
     * @dev Proposes a new admin for the contract. Replaces the proposed admin if there is one.
     * Can only be called by either admin or owner.
     */
    function proposeAdmin(address newAdmin) public virtual {
        require(newAdmin != address(0), "newAdmin is zero address");

        _authorizeAdminAction();

        _state.proposedAdmin = newAdmin;
        emit NewAdminProposed(_state.admin, newAdmin);
    }

    /**
     * @dev The proposed admin accepts the admin transfer.
     */
    function acceptAdmin() external {
        if (msg.sender != _state.proposedAdmin)
            revert SchedulerErrors.Unauthorized();

        address oldAdmin = _state.admin;
        _state.admin = msg.sender;

        _state.proposedAdmin = address(0);
        emit NewAdminAccepted(oldAdmin, msg.sender);
    }

    /**
     * @dev Authorization check for admin actions
     * Must be implemented by the inheriting contract.
     */
```
