### Title
Deposit Limit Check Compares `msg.value` Instead of Cumulative Balance, Allowing Permanent Subscriptions to Exceed `MAX_DEPOSIT_LIMIT` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `MAX_DEPOSIT_LIMIT` guard for permanent subscriptions in `Scheduler.sol` checks only the **single-transaction deposit amount** (`msg.value`) against the cap, not the **cumulative balance** (`status.balanceInWei + msg.value`). Any user can call `addFunds` repeatedly with amounts just at or below `MAX_DEPOSIT_LIMIT` to accumulate an unbounded balance in a permanent subscription, completely defeating the cap.

---

### Finding Description

In both `createSubscription` and `addFunds`, the deposit limit check for permanent subscriptions is:

```solidity
// createSubscription (line 48)
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}

// addFunds (line 613)
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

`MAX_DEPOSIT_LIMIT` is `100 ether`. [1](#0-0) [2](#0-1) [3](#0-2) 

The check validates only the **increment** (`msg.value`) in isolation. After the check passes, the balance is unconditionally accumulated:

```solidity
status.balanceInWei += msg.value;
``` [4](#0-3) 

The correct check should be against the **post-deposit cumulative balance**: `status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT`.

---

### Impact Explanation

Permanent subscriptions are designed to be irrevocable — `withdrawFunds` unconditionally reverts for them:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [5](#0-4) 

The `MAX_DEPOSIT_LIMIT` exists precisely to bound how much ETH can be permanently locked per subscription. Because the check is per-transaction rather than cumulative, the cap is entirely ineffective. An attacker can lock an arbitrary amount of ETH permanently in the contract, far exceeding the intended 100 ETH ceiling.

---

### Likelihood Explanation

The entry path requires no privilege — `addFunds` is callable by anyone (`external payable`, no `onlyManager` modifier):

```solidity
function addFunds(uint256 subscriptionId) external payable override {
``` [6](#0-5) 

Any user who creates a permanent subscription (or any third party who calls `addFunds` on an existing one) can exploit this immediately with no preconditions beyond having ETH.

---

### Recommendation

Change both checks to compare the **post-deposit cumulative balance** against the limit:

```solidity
// createSubscription
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    // change to:
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
```

Correct form:

```solidity
// createSubscription
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) { ... }
// (already correct at creation since balanceInWei starts at 0 — msg.value == post-deposit balance)

// addFunds — MUST be fixed:
if (params.isPermanent && status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

Note: `createSubscription` initializes `balanceInWei = msg.value` from zero, so the check there is accidentally correct for the first deposit. The critical flaw is in `addFunds`, where `status.balanceInWei` already holds prior deposits.

---

### Proof of Concept

```
MAX_DEPOSIT_LIMIT = 100 ether

Step 1: createSubscription{value: 100 ether}(permanentParams)
  → check: 100 ether > 100 ether → false → passes
  → status.balanceInWei = 100 ether

Step 2: addFunds{value: 100 ether}(subscriptionId)
  → check: msg.value (100 ether) > MAX_DEPOSIT_LIMIT (100 ether) → false → passes
  → status.balanceInWei = 200 ether

Step 3: addFunds{value: 100 ether}(subscriptionId)  [repeat N times]
  → status.balanceInWei = N × 100 ether

Result: Unlimited ETH permanently locked, MAX_DEPOSIT_LIMIT completely bypassed.
``` [7](#0-6)

### Citations

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
