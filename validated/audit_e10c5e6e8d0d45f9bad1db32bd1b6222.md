### Title
Funds Permanently Locked in Permanent Subscriptions With No Recovery Path — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract supports "permanent" subscriptions (`isPermanent = true`). Any ETH deposited into such a subscription is irrecoverable: the `withdrawFunds` function unconditionally reverts for permanent subscriptions, `updateSubscription` is also blocked, and `SchedulerGovernance` contains no admin emergency-withdrawal function for subscription balances. Once a permanent subscription's balance falls below the keeper-fee threshold, the remaining ETH is locked forever.

---

### Finding Description

`createSubscription` accepts `isPermanent` as a user-supplied flag and stores it without restriction: [1](#0-0) 

`withdrawFunds` hard-reverts for any permanent subscription, regardless of its state: [2](#0-1) 

`updateSubscription` is equally blocked, so the manager cannot flip `isPermanent` back to `false` to unlock the withdrawal path: [3](#0-2) 

`SchedulerGovernance` provides only fee-parameter setters and admin-transfer functions — there is no function that lets the admin or owner drain or return a subscription's balance: [4](#0-3) 

When a permanent subscription's balance is exhausted to the point where `_processFeesAndPayKeeper` reverts with `InsufficientBalance`, any residual ETH in `status.balanceInWei` has no exit path: [5](#0-4) 

---

### Impact Explanation

ETH deposited into a permanent subscription is permanently locked once the subscription can no longer afford keeper fees. There is no mechanism — for the subscription manager, the admin, or the contract owner — to recover the residual balance. This is a direct loss of user funds with no mitigation path inside the protocol.

---

### Likelihood Explanation

Any unprivileged user can trigger this condition by calling `createSubscription` with `isPermanent = true` and supplying ETH. Because keeper fees include a variable gas-cost component (`gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice`), the balance will inevitably reach a point where it is non-zero but below the next keeper fee, leaving a stranded residual. This is a normal operational outcome for every permanent subscription, not an edge case. [6](#0-5) 

---

### Recommendation

Add an admin-controlled emergency-withdrawal function in `SchedulerGovernance` that can recover the balance of a subscription whose balance has fallen below the minimum threshold (or that has been inactive for a configurable period). Alternatively, allow the subscription manager to withdraw residual funds from a permanent subscription once its balance drops below `getMinimumBalance()`, since at that point the subscription can no longer function anyway.

---

### Proof of Concept

1. Alice calls `createSubscription({isPermanent: true, ...})` with `msg.value = 1 ether`.
2. Keepers call `updatePriceFeeds` repeatedly; each call deducts `pythFee + gasCost + keeperSpecificFee` from `status.balanceInWei`.
3. Eventually `status.balanceInWei` drops to, say, `0.001 ether` — below the next keeper fee. `updatePriceFeeds` now reverts with `InsufficientBalance`.
4. Alice calls `withdrawFunds(subscriptionId, 0.001 ether)` → reverts with `CannotUpdatePermanentSubscription`.
5. Alice calls `updateSubscription(subscriptionId, paramsWithIsPermanentFalse)` → reverts with `CannotUpdatePermanentSubscription`.
6. No admin function exists in `SchedulerGovernance` to recover the balance.
7. The `0.001 ether` is permanently locked in the contract. [7](#0-6) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L32-74)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L846-857)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L1-90)
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
    function _authorizeAdminAction() internal virtual;

    /**
     * @dev Set the keeper fee for single updates in Wei.
     * Calls {_authorizeAdminAction}.
     * Emits a {SingleUpdateKeeperFeeSet} event.
     */
    function setSingleUpdateKeeperFeeInWei(uint128 newFee) external {
        _authorizeAdminAction();

        uint oldFee = _state.singleUpdateKeeperFeeInWei;
        _state.singleUpdateKeeperFeeInWei = newFee;

        emit SingleUpdateKeeperFeeSet(oldFee, newFee);
    }

    /**
     * @dev Set the minimum balance required per feed in a subscription.
     * Calls {_authorizeAdminAction}.
     * Emits a {MinimumBalancePerFeedSet} event.
     */
    function setMinimumBalancePerFeed(uint128 newMinimumBalance) external {
        _authorizeAdminAction();

        uint oldBalance = _state.minimumBalancePerFeed;
        _state.minimumBalancePerFeed = newMinimumBalance;

        emit MinimumBalancePerFeedSet(oldBalance, newMinimumBalance);
    }
}
```
