### Title
Deposit Limit Bypass via Wrong Variable in `addFunds` Validation — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, the `addFunds` function checks `msg.value` (the single-transaction deposit amount) against `MAX_DEPOSIT_LIMIT` for permanent subscriptions, instead of checking the post-deposit total balance `status.balanceInWei`. This mirrors the Aave analog exactly: the wrong variable is used in a validation check. The result is that the deposit cap for permanent subscriptions is trivially bypassed through repeated deposits, allowing funds to be permanently locked in the contract far beyond the intended limit.

---

### Finding Description

In `addFunds`, the deposit-limit guard for permanent subscriptions is:

```solidity
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
``` [1](#0-0) 

The check evaluates only the **current transaction's value** (`msg.value`), not the **resulting total balance** (`status.balanceInWei` after the addition). Because `addFunds` carries no `onlyManager` modifier and is callable by anyone, any caller can invoke it repeatedly with `msg.value = MAX_DEPOSIT_LIMIT - 1 wei`, incrementing `status.balanceInWei` without bound while every individual call passes the guard.

By contrast, `createSubscription` sets `status.balanceInWei = msg.value` at creation time, so the check there is equivalent to checking the total balance — the bug only manifests in the subsequent `addFunds` path. [2](#0-1) 

The correct guard should be:

```solidity
if (params.isPermanent && status.balanceInWei > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

(evaluated **after** `status.balanceInWei += msg.value`).

---

### Impact Explanation

Permanent subscriptions explicitly prohibit withdrawals:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [3](#0-2) 

Any ETH deposited into a permanent subscription is irrecoverably locked; it can only exit via keeper fee payments. By bypassing `MAX_DEPOSIT_LIMIT`, an attacker (or a user who misunderstands the limit) can permanently lock an unbounded amount of ETH in the contract. The contract's ETH holdings grow without the intended cap, increasing the value at risk from any future contract-level exploit and permanently destroying the depositor's funds beyond the protocol-intended ceiling.

---

### Likelihood Explanation

- `addFunds` is `external payable` with **no access control** — any address can call it for any subscription ID.
- The bypass requires only repeated calls with `msg.value < MAX_DEPOSIT_LIMIT`; no special knowledge or privilege is needed.
- The subscription manager themselves may inadvertently trigger this when topping up a permanent subscription after keeper fees have been deducted. [4](#0-3) 

---

### Recommendation

Move the deposit-limit check to **after** the balance update and compare against the resulting total balance:

```solidity
status.balanceInWei += msg.value;

if (params.isPermanent && status.balanceInWei > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

This mirrors the intent of the guard in `createSubscription` and closes the multi-call bypass.

---

### Proof of Concept

1. Deploy `Scheduler` with `MAX_DEPOSIT_LIMIT = 100 ether`.
2. Create a permanent subscription with `msg.value = 100 ether` (passes the creation check).
3. Call `addFunds{value: 99 ether}(subscriptionId)` — passes because `msg.value (99) < MAX_DEPOSIT_LIMIT (100)`.
4. `status.balanceInWei` is now `199 ether`, exceeding `MAX_DEPOSIT_LIMIT`.
5. Repeat step 3 indefinitely; the balance grows without bound while every call succeeds.
6. All deposited ETH is permanently locked (withdrawal reverts with `CannotUpdatePermanentSubscription`). [5](#0-4)

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
