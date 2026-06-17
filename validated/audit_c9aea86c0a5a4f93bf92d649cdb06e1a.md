### Title
Irreversible `isPermanent` Flag Permanently Locks Deposited ETH With No Recovery Path — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract contains an `isPermanent` subscription flag that, once set to `true` by the subscription manager, can never be reversed. While `addFunds()` has **no access control** (anyone can deposit ETH into a permanent subscription), `withdrawFunds()` is unconditionally blocked for permanent subscriptions. There is no admin escape hatch or governance override. All ETH deposited into a permanent subscription — including funds sent by third parties — is permanently locked in the contract.

---

### Finding Description

The `Scheduler.sol` contract manages price-feed subscriptions funded with ETH. Subscriptions have a `SubscriptionParams.isPermanent` boolean flag. The vulnerability arises from three interacting properties:

**1. `isPermanent` is a one-way door.**

`updateSubscription()` checks `currentParams.isPermanent` and reverts with `CannotUpdatePermanentSubscription` for **any** update attempt — including attempts to set `isPermanent = false`, deactivate the subscription, or change any other parameter: [1](#0-0) 

**2. `addFunds()` has no access control.**

Any address — not just the subscription manager — can deposit ETH into any active subscription, including permanent ones: [2](#0-1) 

The test suite explicitly confirms this behavior with the comment `// Anyone can add funds (not just manager)`: [3](#0-2) 

**3. `withdrawFunds()` is unconditionally blocked for permanent subscriptions.**

The very first check in `withdrawFunds()` reverts if `isPermanent` is set, with no admin override or governance escape: [4](#0-3) 

There is no `emergencyWithdraw`, no admin recovery function, and no governance path to unlock funds in `SchedulerGovernance.sol` or `SchedulerUpgradeable.sol`.

---

### Impact Explanation

Any ETH deposited into a permanent subscription is **permanently and irrecoverably locked** in the contract. This affects:

1. **The subscription manager's own funds** — a manager who sets `isPermanent = true` (e.g., to signal commitment or prevent accidental deactivation) permanently forfeits all current and future balance in that subscription.
2. **Third-party ETH** — because `addFunds()` has no access control, any address can send ETH to a permanent subscription. That ETH is also permanently locked. This enables a griefing attack where an attacker forces ETH into a permanent subscription to permanently destroy it.

The ETH is not stolen — it remains in the contract — but it is unrecoverable by any party, including the admin. This constitutes permanent loss of user funds.

---

### Likelihood Explanation

**Medium.** The `isPermanent` flag is a user-facing feature explicitly documented in the subscription parameters struct. A subscription manager who sets it — perhaps to protect a critical subscription from accidental deactivation — will not expect it to permanently forfeit their deposited ETH. The flag's name implies permanence of the *subscription*, not permanence of the *fund lock*. The asymmetry between open deposits and blocked withdrawals is non-obvious. Additionally, the no-access-control `addFunds()` path means any third party can trigger the fund-locking for a permanent subscription without the manager's consent.

---

### Recommendation

1. **Block `addFunds()` for permanent subscriptions entirely**, or restrict it to the subscription manager only. There is no legitimate reason for an arbitrary third party to add funds to a subscription they do not control.
2. **Provide an admin/governance escape hatch** in `SchedulerGovernance.sol` to recover funds from permanent subscriptions in emergency scenarios (e.g., contract migration, critical bug).
3. **Alternatively, allow the manager to withdraw excess funds** (above the minimum balance) even from permanent subscriptions, since the intent of `isPermanent` is to prevent *deactivation*, not to permanently lock capital.
4. Add a clear warning in the `createSubscription` and `updateSubscription` interfaces that setting `isPermanent = true` is irreversible and permanently prevents fund withdrawal.

---

### Proof of Concept

```
1. Alice calls createSubscription{value: 1 ether}(params) where params.isPermanent = false.
   → subscriptionId = 1, balanceInWei = 1 ether, manager = Alice

2. Alice calls updateSubscription(1, params_with_isPermanent_true).
   → isPermanent is now true. This cannot be undone.

3. Bob (arbitrary address) calls addFunds{value: 5 ether}(1).
   → No access control check. balanceInWei = 6 ether. Bob's 5 ETH is now locked.

4. Alice calls withdrawFunds(1, 1 ether).
   → Reverts: CannotUpdatePermanentSubscription

5. Alice calls updateSubscription(1, params_with_isPermanent_false).
   → Reverts: CannotUpdatePermanentSubscription

6. Alice calls updateSubscription(1, params_with_isActive_false).
   → Reverts: CannotUpdatePermanentSubscription

Result: 6 ETH is permanently locked in the Scheduler contract with no recovery path.
``` [5](#0-4) [6](#0-5) [2](#0-1) [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-628)
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

        // If subscription is active, ensure minimum balance is maintained
        if (params.isActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(params.priceIds.length)
            );
            if (status.balanceInWei < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }
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

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L902-904)
```text
        // Anyone can add funds (not just manager)
        vm.prank(address(0x123));
        scheduler.addFunds{value: extraFunds}(subscriptionId);
```
