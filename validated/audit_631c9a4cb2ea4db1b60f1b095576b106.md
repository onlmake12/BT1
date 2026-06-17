### Title
`MAX_DEPOSIT_LIMIT` for Permanent Subscriptions Bypassed via Repeated Small Deposits — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `addFunds` function in `Scheduler.sol` checks only whether the **current transaction's `msg.value`** exceeds `MAX_DEPOSIT_LIMIT`, not whether the **cumulative balance after the deposit** would exceed it. An unprivileged caller can bypass the limit entirely by making multiple deposits each ≤ `MAX_DEPOSIT_LIMIT`, permanently locking an unbounded amount of ETH in a permanent subscription from which withdrawals are prohibited.

---

### Finding Description

`Scheduler.sol` defines a `MAX_DEPOSIT_LIMIT = 100 ether` constant intended to cap how much ETH can be permanently locked in a single permanent subscription.

In `createSubscription`, the check is applied correctly against `msg.value`: [1](#0-0) 

In `addFunds`, the same per-transaction check is used: [2](#0-1) 

The check is:
```solidity
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
```

It validates only the **incoming deposit amount**, not `status.balanceInWei + msg.value`. A caller who deposits exactly `MAX_DEPOSIT_LIMIT` (100 ETH) at creation, then calls `addFunds` with 1 ETH at a time, passes the check on every call while the cumulative balance grows without bound.

Permanent subscriptions explicitly prohibit withdrawals: [3](#0-2) 

`addFunds` has no access-control modifier — any address can call it on any subscription: [4](#0-3) 

The constant and its stated purpose: [5](#0-4) 

---

### Impact Explanation

ETH deposited into a permanent subscription is **irrecoverable** — `withdrawFunds` unconditionally reverts for permanent subscriptions. The `MAX_DEPOSIT_LIMIT` is the only safeguard against excessive permanent locking. Because the check is per-transaction rather than cumulative, it is trivially bypassed:

- A subscription manager can lock far more than 100 ETH permanently in their own subscription.
- Because `addFunds` is permissionless, any third party can also call it on an existing permanent subscription, permanently locking their own ETH in someone else's subscription (griefing themselves while inflating the subscription's balance beyond the intended cap).

The restriction is not working as intended and can be bypassed by anyone with no special privileges.

---

### Likelihood Explanation

The entry point (`addFunds`) is public, payable, and requires no privileged role. The bypass requires only sending multiple transactions with `msg.value ≤ MAX_DEPOSIT_LIMIT`. Any subscription manager who wants to fund a permanent subscription beyond 100 ETH will naturally discover this path. Likelihood is **high**.

---

### Recommendation

Change the `addFunds` check to validate the **post-deposit cumulative balance**:

```solidity
// Before (vulnerable):
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;

// After (fixed):
status.balanceInWei += msg.value;
if (params.isPermanent && status.balanceInWei > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

Apply the same cumulative check in `createSubscription` for consistency (it is already correct there since it is a one-time call, but the pattern should be uniform).

---

### Proof of Concept

```solidity
// Assume MAX_DEPOSIT_LIMIT = 100 ether
// 1. Create a permanent subscription at the limit
uint256 subId = scheduler.createSubscription{value: 100 ether}(permanentParams);

// 2. Verify balance is at the limit
(, SchedulerStructs.SubscriptionStatus memory s) = scheduler.getSubscription(subId);
assert(s.balanceInWei == 100 ether);

// 3. Each call with msg.value <= MAX_DEPOSIT_LIMIT passes the check
scheduler.addFunds{value: 100 ether}(subId); // balance = 200 ETH — no revert
scheduler.addFunds{value: 100 ether}(subId); // balance = 300 ETH — no revert
// ... repeat N times to lock N*100 ETH permanently

// 4. Withdrawal is permanently blocked
scheduler.withdrawFunds(subId, 1 ether); // reverts: CannotUpdatePermanentSubscription
```

The `MAX_DEPOSIT_LIMIT` restriction is completely bypassed; the cumulative balance is never checked against the cap.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-604)
```text
    function addFunds(uint256 subscriptionId) external payable override {
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L612-617)
```text
        // Check deposit limit for permanent subscriptions
        if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        status.balanceInWei += msg.value;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L11-12)
```text
    /// Maximum deposit limit for permanent subscriptions in wei
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```
