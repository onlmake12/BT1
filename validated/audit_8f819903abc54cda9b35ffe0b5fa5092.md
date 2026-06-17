### Title
Permanent Subscription Funds Are Irreversibly Locked With No Withdrawal Path - (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, any user who creates or funds a **permanent subscription** (`isPermanent = true`) permanently locks their ETH in the contract. `withdrawFunds()` unconditionally reverts for permanent subscriptions, `updateSubscription()` blocks all modifications including deactivation, and there is no admin rescue path. The ETH is only consumed over time by keeper fees; any remaining balance is irrecoverable.

---

### Finding Description

The `Scheduler` contract exposes two balance-management functions:

**`addFunds()`** — has no access control; anyone can call it to deposit ETH into any active subscription, including permanent ones. [1](#0-0) 

**`withdrawFunds()`** — unconditionally reverts with `CannotUpdatePermanentSubscription` when `params.isPermanent` is true, with no exception for the subscription manager or any admin. [2](#0-1) 

**`updateSubscription()`** — also unconditionally reverts for permanent subscriptions, blocking deactivation (which would otherwise allow full withdrawal). [3](#0-2) 

The `isPermanent` flag is a one-way ratchet: once set, it cannot be unset.

The `MAX_DEPOSIT_LIMIT` constant is 100 ETH per call. [4](#0-3) 

This means:
1. A manager creates a permanent subscription depositing up to 100 ETH.
2. The `isPermanent` flag can never be cleared.
3. `withdrawFunds()` always reverts for this subscription.
4. `updateSubscription()` always reverts, so the subscription cannot be deactivated to unlock the minimum-balance restriction.
5. Any third party can call `addFunds()` (no access control) to add more ETH to the permanent subscription — those funds are also permanently locked.
6. There is no admin emergency-withdrawal function in the contract.

The only way ETH leaves the contract is through keeper fees charged on each `updatePriceFeeds()` call. Any balance remaining when the subscription is no longer useful is permanently irrecoverable. [5](#0-4) 

---

### Impact Explanation

ETH deposited into a permanent subscription is permanently locked in the `Scheduler` contract with no recovery path for the subscription manager. The manager loses all deposited funds beyond what is consumed by keeper fees. A third party can also grief a permanent subscription by calling `addFunds()` to donate ETH that is then also permanently locked.

This is a direct loss-of-funds vulnerability: up to 100 ETH per `createSubscription` call, plus unlimited additional ETH via repeated `addFunds()` calls (each capped at 100 ETH), are permanently irrecoverable.

---

### Likelihood Explanation

Any user who:
- Creates a permanent subscription and later decides to stop using it, or
- Makes a mistake when setting `isPermanent = true`

will lose their deposited ETH. The `isPermanent` flag is explicitly designed to be irreversible, and the `withdrawFunds()` block is intentional, but no escape hatch (e.g., admin rescue, time-lock expiry, or manager override) exists. This is a realistic scenario for any integrator who uses permanent subscriptions. [6](#0-5) 

---

### Recommendation

Add at least one of the following:

1. **Admin emergency withdrawal**: Allow the contract admin to withdraw funds from a permanent subscription to the subscription manager's address.
2. **Manager override with time-lock**: Allow the subscription manager to initiate a withdrawal from a permanent subscription after a mandatory delay (e.g., 30 days), giving keepers time to drain the balance via updates.
3. **Refund on deactivation**: Allow permanent subscriptions to be deactivated by the manager with a mandatory notice period, after which remaining funds are returned.

---

### Proof of Concept

```solidity
// 1. Manager creates a permanent subscription, depositing 10 ETH
SchedulerStructs.SubscriptionParams memory params = ...;
params.isPermanent = true;
uint256 subId = scheduler.createSubscription{value: 10 ether}(params);

// 2. Manager later tries to withdraw — always reverts
scheduler.withdrawFunds(subId, 1 ether);
// → CannotUpdatePermanentSubscription

// 3. Manager tries to deactivate to unlock minimum balance — always reverts
params.isActive = false;
scheduler.updateSubscription(subId, params);
// → CannotUpdatePermanentSubscription

// 4. Third party can grief by adding more locked ETH
vm.prank(address(0xdead));
scheduler.addFunds{value: 5 ether}(subId);
// → succeeds, 5 ETH now also permanently locked

// 5. No admin rescue function exists — funds are permanently stuck
``` [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-662)
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L12-12)
```text
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```
