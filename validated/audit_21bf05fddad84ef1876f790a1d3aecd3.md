### Title
Permanent Subscription Deposit Limit Bypassed via Incremental `addFunds` Calls — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `addFunds` function in `Scheduler.sol` checks only whether the **single incoming deposit** (`msg.value`) exceeds `MAX_DEPOSIT_LIMIT`, not whether the **cumulative balance** (`status.balanceInWei + msg.value`) exceeds it. Because the balance is updated *after* the check, the existing balance is never considered. An unprivileged user can call `addFunds` repeatedly with amounts just below `MAX_DEPOSIT_LIMIT` to accumulate a balance far exceeding the intended cap on a permanent subscription, permanently locking excess ETH with no withdrawal path.

---

### Finding Description

In `Scheduler.sol`, the `addFunds` function enforces the deposit limit for permanent subscriptions as follows:

```solidity
// Check deposit limit for permanent subscriptions
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}

status.balanceInWei += msg.value;
``` [1](#0-0) 

The guard evaluates `msg.value > MAX_DEPOSIT_LIMIT` in isolation. At the moment of the check, `status.balanceInWei` already holds the previously accumulated balance, but it is **not included in the comparison**. The new deposit is added to the balance only on the line *after* the check passes. This is structurally identical to the H-24 pattern: the check is performed against state that does not yet reflect the item being added.

The same single-value check appears in `createSubscription`:

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [2](#0-1) 

For creation this is harmless (balance starts at zero), but the same flawed pattern in `addFunds` is exploitable because the balance is non-zero after the first deposit.

---

### Impact Explanation

Permanent subscriptions are explicitly designed to be non-withdrawable:

```solidity
// Prevent withdrawals from permanent subscriptions
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [3](#0-2) 

`MAX_DEPOSIT_LIMIT` is the sole safeguard preventing an arbitrarily large amount of ETH from being permanently locked in such a subscription. By bypassing it, a user can irrecoverably lock ETH far in excess of the intended cap. The excess funds are permanently trapped in the contract with no recovery mechanism, constituting a direct loss of user funds.

---

### Likelihood Explanation

The entry point is `addFunds`, a public, payable function with no access control beyond requiring the subscription to be active. Any externally owned account can call it. The bypass requires only repeated calls with `msg.value` just below `MAX_DEPOSIT_LIMIT` — no special privileges, no leaked keys, no governance majority. The only prerequisite is owning a permanent subscription, which any user can create via `createSubscription`.

---

### Recommendation

Replace the single-value check in `addFunds` with a post-addition cumulative check:

```solidity
status.balanceInWei += msg.value;

// Check deposit limit for permanent subscriptions (cumulative)
if (params.isPermanent && status.balanceInWei > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

This mirrors the correct fix for H-24: evaluate the limit **after** the new value has been incorporated into the running total, not before.

---

### Proof of Concept

Given `MAX_DEPOSIT_LIMIT = L` (e.g., 100 ETH):

1. Alice calls `createSubscription` with `isPermanent = true` and `msg.value = L`. Check: `L > L` → false → passes. Balance = `L`.
2. Alice calls `addFunds` with `msg.value = L - 1`. Check: `(L-1) > L` → false → passes. Balance = `2L - 1`.
3. Alice calls `addFunds` again with `msg.value = L - 1`. Check: `(L-1) > L` → false → passes. Balance = `3L - 2`.
4. After `N` additional calls: Balance = `L + N*(L-1)`.

For `N = 9` and `L = 100 ETH`: Balance = `100 + 9*99 = 991 ETH`, nearly 10× the intended cap. All 991 ETH are permanently locked because `withdrawFunds` unconditionally reverts for permanent subscriptions. [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-641)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
```
