### Title
Irreversible `isPermanent` Flag Permanently Locks Subscription Manager Funds With No Recovery Path — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, once a subscription's `isPermanent` flag is set to `true`, it can never be unset. Every subsequent call to `updateSubscription()` and `withdrawFunds()` unconditionally reverts with `CannotUpdatePermanentSubscription`. There is no admin/governance override. Additionally, `addFunds()` has no access control, allowing any third party to permanently lock additional ETH into a permanent subscription with no recovery path for either the manager or the depositor.

---

### Finding Description

`SubscriptionParams.isPermanent` is a boolean field in `SchedulerStructs.sol`: [1](#0-0) 

Any subscription manager can set it to `true` either at creation (`createSubscription`) or via `updateSubscription`. Once set, `updateSubscription` performs a hard gate at the very top of its logic: [2](#0-1) 

This single check blocks **all** mutations — including unsetting `isPermanent` itself, deactivating the subscription, or changing any parameter. There is no path to reverse the flag.

`withdrawFunds()` has its own identical hard gate: [3](#0-2) 

`SchedulerGovernance.sol` provides no admin override for `isPermanent` — it only exposes `setSingleUpdateKeeperFeeInWei` and `setMinimumBalancePerFeed`: [4](#0-3) 

Compounding the issue, `addFunds()` is intentionally unrestricted (no `onlyManager` modifier), so any address can deposit ETH into any active subscription: [5](#0-4) 

For permanent subscriptions, these third-party deposits are also permanently locked. The per-call `MAX_DEPOSIT_LIMIT` check (`100 ether`) only limits a single call's value, not the cumulative balance, so repeated calls can push the locked balance well above 100 ETH. [6](#0-5) 

---

### Impact Explanation

A subscription manager who sets `isPermanent = true` — whether intentionally or by mistake — permanently and irrecoverably loses access to all ETH deposited in that subscription. There is no admin escape hatch, no time-locked recovery, and no governance action that can override the flag. Any third party who calls `addFunds()` on a permanent subscription also permanently loses their ETH. The `MAX_DEPOSIT_LIMIT` of 100 ETH per call means the locked amount can be substantial. [7](#0-6) 

---

### Likelihood Explanation

The entry path requires no privilege — any user who creates a subscription can trigger this. The `isPermanent` flag is user-controlled and is documented as a feature, meaning users are actively encouraged to use it. A user who sets `isPermanent = true` expecting to be able to recover funds later (e.g., if the subscription becomes obsolete or the price feed is deprecated) will find their ETH permanently locked. The unrestricted `addFunds()` griefing vector is reachable by any unprivileged address. [8](#0-7) 

---

### Recommendation

1. Add an admin/governance override function in `SchedulerGovernance.sol` that can unset `isPermanent` or trigger an emergency fund recovery for a specific subscription.
2. Alternatively, allow the subscription manager to unset `isPermanent` subject to a time-lock or admin co-signature, analogous to how the external report recommends passing a boolean to toggle the blocked state.
3. Restrict `addFunds()` for permanent subscriptions to `onlyManager`, or add a separate `donateToSubscription()` path that explicitly warns callers the funds are non-recoverable.

---

### Proof of Concept

```solidity
// 1. Manager creates a subscription and sets isPermanent = true
SchedulerStructs.SubscriptionParams memory params = ...;
params.isPermanent = true;
uint256 subId = scheduler.createSubscription{value: 10 ether}(params);

// 2. Manager later tries to withdraw — permanently reverts
scheduler.withdrawFunds(subId, 10 ether);
// => CannotUpdatePermanentSubscription

// 3. Manager tries to unset isPermanent — permanently reverts
params.isPermanent = false;
scheduler.updateSubscription(subId, params);
// => CannotUpdatePermanentSubscription

// 4. Any third party can lock additional ETH with no recovery
vm.prank(address(0xBEEF));
scheduler.addFunds{value: 50 ether}(subId);
// 50 ETH is now permanently locked; 0xBEEF cannot recover it
``` [9](#0-8) [10](#0-9)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L14-14)
```text
        bool isPermanent; // Whether the subscription can be updated
```

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L77-92)
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

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L68-89)
```text
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
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L12-12)
```text
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```
