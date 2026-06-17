### Title
Permanent Subscription Balance Cap Bypass via Cumulative `addFunds` Calls — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `MAX_DEPOSIT_LIMIT` check in `Scheduler.addFunds` validates only the incoming `msg.value` against the cap, not the resulting cumulative balance. Because permanent subscriptions prohibit withdrawals, this allows any caller to permanently lock an unbounded amount of ETH in a single permanent subscription, fully bypassing the 100 ETH protocol cap.

---

### Finding Description

`SchedulerConstants.sol` defines `MAX_DEPOSIT_LIMIT = 100 ether` as the ceiling for funds held in a permanent subscription. [1](#0-0) 

In `addFunds`, the guard is:

```solidity
// Check deposit limit for permanent subscriptions
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
``` [2](#0-1) 

The condition `msg.value > MAX_DEPOSIT_LIMIT` tests only the **new deposit in isolation**. It never compares `status.balanceInWei + msg.value` against the cap. Consequently, after an initial deposit of exactly `MAX_DEPOSIT_LIMIT`, every subsequent call to `addFunds` with any amount ≤ `MAX_DEPOSIT_LIMIT` succeeds, growing `balanceInWei` without bound.

The same off-by-one pattern exists in `createSubscription`: [3](#0-2) 

`addFunds` carries no `onlyManager` modifier, so **any address** — not just the subscription owner — can trigger the bypass on any permanent subscription. [4](#0-3) 

Permanent subscriptions explicitly forbid withdrawals: [5](#0-4) 

So any ETH deposited beyond the cap is irrecoverably locked.

The existing test `testPermanentSubscriptionDepositLimit` only verifies that a single deposit exceeding the limit reverts; it never tests the cumulative case, leaving the bypass undetected. [6](#0-5) 

---

### Impact Explanation

- The `MAX_DEPOSIT_LIMIT` invariant for permanent subscriptions is completely broken.
- An attacker (or the subscription owner) can lock an arbitrary amount of ETH permanently in a single subscription with no recovery path.
- Because `addFunds` is permissionless, a third party can force this condition on any existing permanent subscription without the manager's consent, permanently inflating its locked balance.

---

### Likelihood Explanation

The entry path requires only a standard ETH transfer to a public, payable function — no privileged role, no leaked key, no governance majority. Any EOA or contract can call `addFunds` on any active permanent subscription. The bypass requires nothing more than calling the function twice with amounts ≤ 100 ETH.

---

### Recommendation

Replace the per-deposit check with a post-addition cumulative check:

```solidity
// Check deposit limit for permanent subscriptions
if (params.isPermanent) {
    uint256 newBalance = status.balanceInWei + msg.value;
    if (newBalance > MAX_DEPOSIT_LIMIT) {
        revert SchedulerErrors.MaxDepositLimitExceeded();
    }
}
status.balanceInWei += msg.value;
```

Apply the same fix to `createSubscription` for consistency (though `createSubscription` initialises `balanceInWei` from zero, so the current check there is technically correct for the creation path alone).

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

import "forge-std/Test.sol";
import "../contracts/pulse/Scheduler.sol"; // adjust path as needed

contract PermanentCapBypassTest is Test {
    SchedulerProxy scheduler;

    function setUp() public {
        // deploy scheduler (omitted for brevity)
    }

    function testBypassMaxDepositLimit() public {
        uint256 cap = scheduler.MAX_DEPOSIT_LIMIT(); // 100 ether

        // Step 1: create a permanent subscription at exactly the cap
        SchedulerStructs.SubscriptionParams memory params = /* ... */;
        params.isPermanent = true;
        vm.deal(address(this), cap);
        uint256 subId = scheduler.createSubscription{value: cap}(params);

        // Step 2: add funds again at exactly the cap — should revert but does NOT
        vm.deal(address(this), cap);
        scheduler.addFunds{value: cap}(subId);

        // Step 3: verify balance is now 200 ether, double the intended cap
        (, SchedulerStructs.SubscriptionStatus memory status) =
            scheduler.getSubscription(subId);
        assertEq(status.balanceInWei, 2 * cap); // 200 ether — cap bypassed
    }
}
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
