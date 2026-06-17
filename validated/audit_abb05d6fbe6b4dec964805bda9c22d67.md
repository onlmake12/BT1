### Title
Permanent Subscription Funds Are Permanently Locked With No Withdrawal Path — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract (Pulse) allows subscription managers to mark subscriptions as `isPermanent = true`. Once set, the subscription's entire ETH balance is permanently locked: `withdrawFunds` unconditionally reverts for permanent subscriptions, no admin override exists, and no governance recovery path is implemented. Any ETH deposited into a permanent subscription — whether at creation or via `addFunds` — is irrecoverable.

---

### Finding Description

The `Scheduler.sol` contract manages subscription-based price feed delivery. Subscribers deposit ETH to fund keeper payments and Pyth update fees. A subscription can be marked permanent via `updateSubscription`.

`withdrawFunds` contains an unconditional guard:

```solidity
// Prevent withdrawals from permanent subscriptions
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [1](#0-0) 

`addFunds` still accepts ETH for permanent subscriptions (only enforcing a `MAX_DEPOSIT_LIMIT` cap):

```solidity
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
``` [2](#0-1) 

`SchedulerGovernance.sol` provides no fund-recovery function — only admin rotation and fee parameter setters: [3](#0-2) 

`SchedulerState.sol` stores no accumulated protocol-fee balance that could be swept; all keeper fees are paid directly to `msg.sender` and Pyth fees are forwarded to the Pyth contract. There is therefore no secondary path to drain the subscription balance: [4](#0-3) 

Once `isPermanent` is set, `updateSubscription` also reverts for any modification: [5](#0-4) 

---

### Impact Explanation

Any ETH balance remaining in a permanent subscription is permanently locked in the contract with no recovery path for the subscription manager, the admin, or governance. Scenarios that strand funds:

1. A subscription manager deposits a large balance, then the subscribed price feeds are delisted or the protocol is deprecated — the remaining balance is unrecoverable.
2. A manager tops up a permanent subscription via `addFunds` after the subscription's update criteria are no longer triggerable (e.g., all price IDs removed from Pyth) — ETH is locked with no keeper ever spending it.
3. A contract upgrade renders the subscription obsolete — no admin sweep function exists.

**Impact: High** — direct, permanent loss of user ETH with no mitigation path.

---

### Likelihood Explanation

The `isPermanent` flag must be explicitly set by the subscription manager via `updateSubscription`. However:

- The flag is a simple boolean in `SubscriptionParams`; a manager can set it inadvertently.
- Once set it is irreversible — the contract provides no "un-permanent" path.
- Any subsequent `addFunds` call silently accepts ETH into the locked balance.

**Likelihood: Medium** — requires an explicit action by the manager, but the irreversibility and silent fund acceptance make accidental permanent locking realistic.

---

### Recommendation

1. Add an admin/governance-controlled emergency withdrawal function that can recover ETH from permanent subscriptions (e.g., `emergencyWithdraw(uint256 subscriptionId, address recipient)`), callable only by the contract owner or via governance.
2. Alternatively, allow permanent subscriptions to be deactivated (but not re-activated) so that `withdrawFunds` can drain the remaining balance after deactivation.
3. Emit a clear warning event when a subscription is marked permanent, and document the irreversibility prominently.

---

### Proof of Concept

1. Subscription manager calls `createSubscription{value: 10 ether}(params)` → `subscriptionId = 1`.
2. Manager calls `updateSubscription(1, params_with_isPermanent_true)` → subscription is now permanent.
3. Manager calls `addFunds{value: 5 ether}(1)` → accepted, balance = 15 ETH.
4. Manager calls `withdrawFunds(1, 1 ether)` → **reverts** with `CannotUpdatePermanentSubscription`.
5. Admin calls any governance function → no fund-recovery function exists.
6. 15 ETH is permanently locked in the contract. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L612-617)
```text
        // Check deposit limit for permanent subscriptions
        if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        status.balanceInWei += msg.value;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-661)
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
