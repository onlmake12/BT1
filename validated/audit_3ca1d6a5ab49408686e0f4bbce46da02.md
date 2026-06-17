### Title
`Scheduler.addFunds()` Deposit Limit for Permanent Subscriptions Checks Per-Transaction `msg.value` Instead of Cumulative Balance, Allowing Unbounded ETH to Be Permanently Locked — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `MAX_DEPOSIT_LIMIT` guard on permanent subscriptions in `Scheduler.sol` checks only the per-transaction `msg.value`, not the cumulative `status.balanceInWei`. Because `addFunds()` has no access control and the check is per-call, any caller can bypass the cap by making multiple deposits, each ≤ `MAX_DEPOSIT_LIMIT`. Since permanent subscriptions prohibit withdrawals, the excess ETH is permanently locked in the contract.

---

### Finding Description

`SchedulerConstants` defines `MAX_DEPOSIT_LIMIT = 100 ether` as a cap for permanent subscriptions. [1](#0-0) 

The guard is applied in two places:

**`createSubscription`:** [2](#0-1) 

**`addFunds`:** [3](#0-2) 

Both checks compare `msg.value` (the single-transaction deposit) against `MAX_DEPOSIT_LIMIT`. Neither check compares the **post-deposit cumulative balance** (`status.balanceInWei`) against the limit. The balance is simply incremented unconditionally after the check passes: [4](#0-3) 

`addFunds` has **no access control** — anyone can call it for any subscription: [5](#0-4) 

This is confirmed by the test suite, which explicitly documents this behavior: [6](#0-5) 

Withdrawals from permanent subscriptions are permanently blocked: [7](#0-6) 

---

### Impact Explanation

The `MAX_DEPOSIT_LIMIT` protection is completely ineffective. An attacker (or the subscription manager themselves) can:

1. Create a permanent subscription with `msg.value = MAX_DEPOSIT_LIMIT` (100 ETH) — passes the check.
2. Call `addFunds(subscriptionId)` repeatedly with `msg.value = MAX_DEPOSIT_LIMIT` each time — each call passes the per-tx check.
3. The cumulative `status.balanceInWei` grows to 200 ETH, 300 ETH, or any arbitrary amount.
4. Since withdrawals are permanently blocked for permanent subscriptions, all deposited ETH beyond the intended cap is irreversibly locked in the contract.

Additionally, because `addFunds` is permissionless, a **griefing attacker** can call it on any victim's permanent subscription, permanently locking the attacker's own ETH (or dust amounts) in the victim's subscription — a denial-of-service against the subscription's intended funding model.

---

### Likelihood Explanation

The attack requires no privileged access, no leaked keys, and no external oracle manipulation. Any unprivileged address can call `addFunds` on any active permanent subscription. The only cost to the attacker is the ETH deposited (which is permanently lost), making griefing attacks economically rational only for low-value amounts. However, the bypass of the cumulative cap is trivially exploitable by the subscription manager themselves, who may simply want to deposit more than 100 ETH into their own permanent subscription.

---

### Recommendation

Replace the per-transaction `msg.value` check with a check against the **post-deposit cumulative balance**:

```solidity
// In addFunds():
status.balanceInWei += msg.value;

if (params.isPermanent && status.balanceInWei > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

Apply the same fix in `createSubscription`. Additionally, consider restricting `addFunds` to the subscription manager for permanent subscriptions to prevent griefing.

---

### Proof of Concept

```solidity
// Assume MAX_DEPOSIT_LIMIT = 100 ether
// 1. Create permanent subscription with exactly 100 ETH (passes check)
uint256 subId = scheduler.createSubscription{value: 100 ether}(permanentParams);

// 2. Call addFunds repeatedly — each call passes the per-tx check
scheduler.addFunds{value: 100 ether}(subId); // balance = 200 ETH
scheduler.addFunds{value: 100 ether}(subId); // balance = 300 ETH
// ... repeat N times

// 3. Attempt withdrawal — permanently blocked
scheduler.withdrawFunds(subId, 1 ether); // reverts: CannotUpdatePermanentSubscription

// Result: (N+1)*100 ETH permanently locked, MAX_DEPOSIT_LIMIT completely bypassed
```

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L11-12)
```text
    /// Maximum deposit limit for permanent subscriptions in wei
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-603)
```text
    function addFunds(uint256 subscriptionId) external payable override {
        SchedulerStructs.SubscriptionParams storage params = _state
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L612-615)
```text
        // Check deposit limit for permanent subscriptions
        if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L617-617)
```text
        status.balanceInWei += msg.value;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1156-1174)
```text
    function testAnyoneCanAddFunds() public {
        // Create a subscription
        uint256 subscriptionId = addTestSubscription(
            scheduler,
            address(reader)
        );

        // Get initial balance
        (, SchedulerStructs.SubscriptionStatus memory initialStatus) = scheduler
            .getSubscription(subscriptionId);
        uint256 initialBalance = initialStatus.balanceInWei;

        // Have a different address add funds
        address funder = address(0x123);
        uint256 fundAmount = 1 ether;
        vm.deal(funder, fundAmount);

        vm.prank(funder);
        scheduler.addFunds{value: fundAmount}(subscriptionId);
```
