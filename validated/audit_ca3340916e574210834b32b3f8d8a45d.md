### Title
`SchedulerGovernance` Admin Cannot Force-Deactivate Permanent Subscriptions - (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `SchedulerGovernance` admin role is documented as the governance authority for the Pulse/Scheduler contract, responsible for "deploying this contract, upgrading it, and configuring system-level parameters." However, neither `SchedulerGovernance` nor any other contract component provides the admin (or owner) with any function to force-deactivate or override a `isPermanent` subscription. Once a subscription is marked permanent, it is irrevocable by anyone — including the Pyth Data Association multisig admin — with no emergency escape hatch.

---

### Finding Description

The `Scheduler` contract supports a `isPermanent` flag on subscriptions. Once set, `updateSubscription` immediately reverts with `CannotUpdatePermanentSubscription` for **any** change, including deactivation: [1](#0-0) 

The `updateSubscription` function is also gated by `onlyManager`, meaning only the subscription creator can call it — and even they cannot deactivate a permanent subscription: [2](#0-1) 

The `withdrawFunds` function similarly blocks permanent subscriptions: [3](#0-2) 

The `SchedulerGovernance` contract — which defines all admin-level actions — contains only four functions: `proposeAdmin`, `acceptAdmin`, `setSingleUpdateKeeperFeeInWei`, and `setMinimumBalancePerFeed`. There is no `deactivateSubscription`, `forceDeactivate`, or any emergency override for permanent subscriptions: [4](#0-3) 

The README explicitly documents the Admin role as the governance authority: [5](#0-4) 

Yet the `SchedulerGovernance` implementation provides no mechanism for the admin to exercise emergency control over permanent subscriptions.

---

### Impact Explanation

A permanent subscription that is misconfigured, abusive, or targets manipulated price feeds will continue operating indefinitely. Keepers will continue draining the subscription's balance. The admin/owner has no on-chain path to stop it short of a full UUPS contract upgrade — a heavy-handed operation requiring the owner multisig, upgrade proposal, and deployment cycle. During that window, the subscription continues consuming funds and keeper resources. This directly impacts protocol availability and the subscription manager's deposited funds.

---

### Likelihood Explanation

Any user can create a permanent subscription (`isPermanent = true`) at subscription creation time or by calling `updateSubscription` to set the flag. This is an unprivileged, permissionless action. Once set, the irreversibility is permanent. The scenario where a permanent subscription needs emergency intervention (e.g., a subscription manager's key is compromised and the subscription is updated to drain funds rapidly before being made permanent, or a subscription targets a price feed that is later found to be manipulated) is realistic.

---

### Recommendation

Add an admin-only emergency override function to `SchedulerGovernance` that can force-deactivate any subscription, including permanent ones:

```solidity
// In SchedulerGovernance.sol
event SubscriptionForceDeactivated(uint256 indexed subscriptionId);

function forceDeactivateSubscription(uint256 subscriptionId) external {
    _authorizeAdminAction();

    SchedulerStructs.SubscriptionParams storage params =
        _state.subscriptionParams[subscriptionId];

    if (!params.isActive) revert SchedulerErrors.InactiveSubscription();

    params.isActive = false;
    _removeFromActiveSubscriptions(subscriptionId);

    emit SubscriptionForceDeactivated(subscriptionId);
}
```

This mirrors the pattern used in the Centrifuge fix (adding `removePauser` to `DelayedAdmin`) — giving the documented governance authority the actual on-chain capability to act in emergencies.

---

### Proof of Concept

1. Any user creates a permanent subscription:
```solidity
SchedulerStructs.SubscriptionParams memory params = ...;
params.isPermanent = true;
uint256 subId = scheduler.createSubscription{value: minBalance}(params);
```

2. The subscription is now permanently active. The admin attempts to deactivate it:
```solidity
vm.prank(admin);
// No such function exists — admin has zero on-chain recourse
// scheduler.forceDeactivateSubscription(subId); // DOES NOT EXIST
```

3. The only path is a UUPS upgrade by the owner:
```solidity
vm.prank(owner);
scheduler.upgradeTo(address(newImplementation)); // Heavy-handed, requires full upgrade cycle
```

The `SchedulerGovernance` admin — documented as the governance authority — has no direct emergency action, confirming the missing capability. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L77-80)
```text
    function updateSubscription(
        uint256 subscriptionId,
        SchedulerStructs.SubscriptionParams memory newParams
    ) external payable override onlyManager(subscriptionId) {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
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

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L25-25)
```markdown
- **Admin:** Controlled by the Pyth Data Association multisig. Responsible for deploying this contract, upgrading it, and configuring system-level parameters.
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerUpgradeable.sol (L53-60)
```text
    /// Only the owner can upgrade the contract
    function _authorizeUpgrade(address) internal override onlyOwner {}

    /// Authorize actions that both admin and owner can perform
    function _authorizeAdminAction() internal view override {
        if (msg.sender != owner() && msg.sender != _state.admin)
            revert SchedulerErrors.Unauthorized();
    }
```
