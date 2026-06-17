### Title
`MAX_DEPOSIT_LIMIT` Not Enforced on Cumulative Balance in `addFunds()` — Permanent Subscriptions Can Accumulate Unbounded ETH - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.addFunds()` checks only whether the **single deposit amount** (`msg.value`) exceeds `MAX_DEPOSIT_LIMIT`, not whether the **resulting cumulative balance** (`status.balanceInWei + msg.value`) exceeds it. Because permanent subscriptions forbid withdrawals and deactivation, any ETH deposited beyond the intended cap is permanently locked in the contract.

---

### Finding Description

`MAX_DEPOSIT_LIMIT` is defined as 100 ETH and is documented as the "Maximum deposit limit for permanent subscriptions in wei." [1](#0-0) 

Both `createSubscription` and `addFunds` enforce this limit, but both check only the **incoming `msg.value`**, not the post-deposit balance:

**`createSubscription`** (line 48):
```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [2](#0-1) 

**`addFunds`** (line 613):
```solidity
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
``` [3](#0-2) 

The check is `msg.value > MAX_DEPOSIT_LIMIT`, not `status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT`. This means:

1. Create a permanent subscription depositing exactly `MAX_DEPOSIT_LIMIT` (100 ETH) — passes.
2. Call `addFunds` with `MAX_DEPOSIT_LIMIT` again — `msg.value == 100 ETH`, which is **not** `> MAX_DEPOSIT_LIMIT`, so it passes. Balance is now 200 ETH.
3. Repeat indefinitely. Balance grows without bound.

Permanent subscriptions have no escape hatch: `withdrawFunds` reverts with `CannotUpdatePermanentSubscription`, `updateSubscription` reverts with the same error, and deactivation is blocked. [4](#0-3) 

`addFunds` has **no access control** — any address can call it on any active subscription. [5](#0-4) 

---

### Impact Explanation

ETH deposited into a permanent subscription is **permanently locked** in the contract. The `MAX_DEPOSIT_LIMIT` is intended to bound this locked amount to 100 ETH per permanent subscription, but the per-transaction check fails to enforce a cumulative cap. An attacker (or even a well-meaning user) can call `addFunds` N times with amounts ≤ 100 ETH each, locking N × 100 ETH permanently. There is no admin recovery path for funds in permanent subscriptions.

---

### Likelihood Explanation

`addFunds` is a public, permissionless function — no role or key is required. The exploit requires only repeated ETH sends, each ≤ 100 ETH. The existing test suite confirms the per-transaction check works but does **not** test the cumulative case, meaning the gap is undetected by current tests. [6](#0-5) 

---

### Recommendation

Change the check in `addFunds` (and optionally harden `createSubscription`) to validate the **post-deposit balance**:

```solidity
// In addFunds:
if (params.isPermanent) {
    if (status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
        revert SchedulerErrors.MaxDepositLimitExceeded();
    }
}
status.balanceInWei += msg.value;
```

This mirrors the correct pattern from the EIP-4626 fix: the cap must be checked against the **total** (existing + incoming), not just the incoming amount.

---

### Proof of Concept

```solidity
// 1. Create permanent subscription with MAX_DEPOSIT_LIMIT (100 ETH)
params.isPermanent = true;
uint256 subId = scheduler.createSubscription{value: 100 ether}(params);
// balanceInWei == 100 ETH

// 2. Call addFunds with exactly MAX_DEPOSIT_LIMIT — passes the check (100 ether is NOT > 100 ether)
scheduler.addFunds{value: 100 ether}(subId);
// balanceInWei == 200 ETH — exceeds the intended cap

// 3. Repeat: call addFunds with 100 ether N more times
// balanceInWei == (N+1) * 100 ETH, all permanently locked

// 4. Confirm no recovery: withdrawFunds reverts
scheduler.withdrawFunds(subId, 1 ether); // reverts: CannotUpdatePermanentSubscription
``` [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-602)
```text
    function addFunds(uint256 subscriptionId) external payable override {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L612-617)
```text
        // Check deposit limit for permanent subscriptions
        if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        status.balanceInWei += msg.value;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-641)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1085-1094)
```text
        // Test 3: Adding funds to a permanent subscription with deposit exceeding MAX_DEPOSIT_LIMIT should fail
        uint256 largeAdditionalFunds = maxDepositLimit + 1;
        vm.deal(address(this), largeAdditionalFunds);

        vm.expectRevert(
            abi.encodeWithSelector(
                SchedulerErrors.MaxDepositLimitExceeded.selector
            )
        );
        scheduler.addFunds{value: largeAdditionalFunds}(subscriptionId);
```
