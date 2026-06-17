I found the vulnerability. Let me read the full relevant section to confirm the root cause before writing the report.

### Title
`MAX_DEPOSIT_LIMIT` Invariant Bypassed for Permanent Subscriptions via Cumulative `addFunds()` Deposits — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol` enforces a `MAX_DEPOSIT_LIMIT` of 100 ETH on permanent subscriptions to cap how much ETH can be irreversibly locked. However, the guard in `addFunds()` compares only the **individual deposit amount** (`msg.value`) against the limit, not the **cumulative balance** (`status.balanceInWei + msg.value`). Because permanent subscriptions can never withdraw, repeated calls to `addFunds()` with amounts individually below the cap can push the total balance arbitrarily above 100 ETH, permanently locking excess ETH in the contract.

---

### Finding Description

`SchedulerConstants.sol` declares:

```solidity
uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
``` [1](#0-0) 

`createSubscription()` enforces this correctly for the initial deposit (balance starts at zero, so `msg.value` equals the new balance):

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [2](#0-1) 

`addFunds()` applies the **same single-deposit check** without accounting for the existing balance:

```solidity
// Check deposit limit for permanent subscriptions
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}

status.balanceInWei += msg.value;
``` [3](#0-2) 

The check should be `status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT`, but it is not. Because `addFunds()` carries no access-control modifier, **any address** (not just the subscription manager) can call it.

`withdrawFunds()` unconditionally reverts for permanent subscriptions:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [4](#0-3) 

So any ETH deposited above the limit is permanently unrecoverable.

---

### Impact Explanation

- **Invariant broken**: The protocol's explicit `MAX_DEPOSIT_LIMIT` for permanent subscriptions is violated; the cumulative balance can grow without bound.
- **Permanent ETH lock**: Because permanent subscriptions cannot withdraw, all excess ETH above 100 ETH is irreversibly locked in the contract.
- **No privileged access required**: `addFunds()` is a public, payable function with no `onlyManager` guard. Any unprivileged address can trigger the bypass by sending ETH to any permanent subscription.

---

### Likelihood Explanation

The path is straightforward and requires no special conditions:

1. A permanent subscription already exists at or near `MAX_DEPOSIT_LIMIT`.
2. Any caller invokes `addFunds(subscriptionId)` with `msg.value` ≤ `MAX_DEPOSIT_LIMIT` (e.g., 1 wei to 100 ETH).
3. The check passes because `msg.value ≤ 100 ETH`, but `status.balanceInWei` now exceeds 100 ETH.

The subscription manager can do this intentionally (to lock more ETH than the protocol allows), or a third party can do it to grief the manager by permanently locking the third party's own ETH into the manager's subscription.

---

### Recommendation

Change the guard in `addFunds()` to check the **post-deposit cumulative balance**:

```diff
- if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
+ if (params.isPermanent && status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
      revert SchedulerErrors.MaxDepositLimitExceeded();
  }
``` [5](#0-4) 

---

### Proof of Concept

```solidity
function test_permanent_deposit_exceeds_max() public {
    // 1. Create a permanent subscription at exactly MAX_DEPOSIT_LIMIT (100 ETH)
    SchedulerStructs.SubscriptionParams memory params = createDefaultSubscriptionParams(2, address(reader));
    params.isPermanent = true;

    uint256 maxLimit = scheduler.MAX_DEPOSIT_LIMIT(); // 100 ether
    vm.deal(address(this), maxLimit);
    uint256 subId = scheduler.createSubscription{value: maxLimit}(params);

    // 2. addFunds with 99 ETH — passes the check because 99 ETH < 100 ETH
    uint256 extraDeposit = 99 ether;
    vm.deal(address(this), extraDeposit);
    scheduler.addFunds{value: extraDeposit}(subId); // does NOT revert

    // 3. Cumulative balance is now 199 ETH — far above MAX_DEPOSIT_LIMIT
    (, SchedulerStructs.SubscriptionStatus memory status) = scheduler.getSubscription(subId);
    assertEq(status.balanceInWei, maxLimit + extraDeposit); // 199 ETH

    // 4. Funds are permanently locked — withdrawal reverts
    vm.expectRevert(SchedulerErrors.CannotUpdatePermanentSubscription.selector);
    scheduler.withdrawFunds(subId, 1 wei);
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
