### Title
`MAX_DEPOSIT_LIMIT` for Permanent Subscriptions Bypassed via Repeated `addFunds` Calls — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `MAX_DEPOSIT_LIMIT` cap on permanent subscriptions in `Scheduler.sol` is enforced only against the **per-transaction `msg.value`**, not against the **cumulative `status.balanceInWei`**. Because `addFunds` is callable by anyone with no access restriction, any caller can bypass the 100 ETH cap by calling `addFunds` repeatedly with amounts ≤ `MAX_DEPOSIT_LIMIT`, permanently locking an unbounded amount of ETH in a permanent subscription.

---

### Finding Description

`Scheduler.sol` defines a `MAX_DEPOSIT_LIMIT = 100 ether` constant intended to cap how much ETH can be deposited into a permanent subscription (which can never be withdrawn from).

The check in `createSubscription`:

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [1](#0-0) 

And the identical check in `addFunds`:

```solidity
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
``` [2](#0-1) 

Both checks validate only the **current transaction's `msg.value`**, not the **running total `status.balanceInWei`**. Since `addFunds` has no access control (no `onlyManager` modifier), any address can call it for any subscription ID.

The `MAX_DEPOSIT_LIMIT` is 100 ETH: [3](#0-2) 

Withdrawals from permanent subscriptions are permanently blocked: [4](#0-3) 

---

### Impact Explanation

The `MAX_DEPOSIT_LIMIT` safety cap is completely ineffective. A subscription manager (or any third party) can permanently lock an unbounded amount of ETH into a single permanent subscription by calling `addFunds` N times with `msg.value = MAX_DEPOSIT_LIMIT` each time. Since permanent subscriptions prohibit withdrawals, all deposited ETH is irrecoverably locked in the contract. The intended invariant — that no permanent subscription can hold more than 100 ETH — is violated.

---

### Likelihood Explanation

The attack path requires only standard ETH and repeated calls to a public, permissionless function. No privileged role, leaked key, or external oracle manipulation is needed. The `testAnyoneCanAddFunds` test explicitly confirms that any address can call `addFunds` on any subscription: [5](#0-4) 

---

### Recommendation

Change the `addFunds` check to validate the **post-deposit cumulative balance** rather than the per-transaction `msg.value`:

```solidity
// Instead of:
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) { ... }

// Use:
if (params.isPermanent && status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) { ... }
```

Apply the same fix to `createSubscription` for consistency (though `createSubscription` initializes `balanceInWei` from zero, so the current check there is technically correct for the creation case alone).

---

### Proof of Concept

1. Alice creates a permanent subscription with `msg.value = 100 ether` — passes the `MAX_DEPOSIT_LIMIT` check. `status.balanceInWei = 100 ether`.
2. Alice calls `addFunds(subscriptionId)` with `msg.value = 100 ether` — `msg.value (100 ether) > MAX_DEPOSIT_LIMIT (100 ether)` is **false**, so no revert. `status.balanceInWei = 200 ether`.
3. Alice repeats step 2 indefinitely. After N calls, `status.balanceInWei = (N+1) * 100 ether`.
4. Since `withdrawFunds` reverts for permanent subscriptions, all ETH is permanently locked.

The cumulative balance is never checked against `MAX_DEPOSIT_LIMIT`, so the cap is trivially bypassed in a single block with no special privileges.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L11-12)
```text
    /// Maximum deposit limit for permanent subscriptions in wei
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L1156-1185)
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

        // Verify funds were added
        (, SchedulerStructs.SubscriptionStatus memory status) = scheduler
            .getSubscription(subscriptionId);

        assertEq(
            status.balanceInWei,
            initialBalance + fundAmount,
            "Balance should be increased by the funded amount"
        );
    }
```
