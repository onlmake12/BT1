### Title
`MAX_DEPOSIT_LIMIT` for Permanent Subscriptions Is Not Enforced Cumulatively in `addFunds`, Allowing Unlimited ETH to Be Permanently Locked — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.addFunds` checks only the per-transaction `msg.value` against `MAX_DEPOSIT_LIMIT`, not the cumulative `status.balanceInWei`. Because `addFunds` has no access control, any caller can repeatedly top up any permanent subscription in increments of up to `MAX_DEPOSIT_LIMIT`, growing the balance without bound. Since permanent subscriptions explicitly forbid withdrawals, all excess ETH is permanently locked in the contract.

---

### Finding Description

`SchedulerConstants.sol` declares the invariant:

```solidity
/// Maximum deposit limit for permanent subscriptions in wei
uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
``` [1](#0-0) 

`createSubscription` enforces this correctly on the initial deposit (where `balanceInWei` equals `msg.value`):

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [2](#0-1) 

`addFunds` repeats the same single-transaction check but never compares the **post-deposit cumulative balance** against the limit:

```solidity
function addFunds(uint256 subscriptionId) external payable override {
    ...
    // Check deposit limit for permanent subscriptions
    if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {   // ← only msg.value
        revert SchedulerErrors.MaxDepositLimitExceeded();
    }
    status.balanceInWei += msg.value;   // ← cumulative balance grows unboundedly
    ...
}
``` [3](#0-2) 

The missing check should be `status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT`.

Compounding the issue, `withdrawFunds` unconditionally reverts for permanent subscriptions:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [4](#0-3) 

So any ETH deposited beyond `MAX_DEPOSIT_LIMIT` is irrecoverably locked.

---

### Impact Explanation

1. **Invariant bypass**: The protocol's stated cap of 100 ETH per permanent subscription is trivially circumvented by any caller.
2. **Permanent ETH lock**: Because `addFunds` has no access control (anyone can fund any subscription) and permanent subscriptions cannot be withdrawn from, an attacker can force-lock arbitrary amounts of ETH into any victim's permanent subscription. The victim cannot recover the excess.
3. **Griefing / fund destruction**: An adversary willing to spend ETH can permanently destroy value by locking it in a target subscription, with no benefit to themselves and no recourse for the victim.

---

### Likelihood Explanation

- `addFunds` is a public, permissionless function — no whitelist, no ownership check.
- Active permanent subscription IDs are publicly enumerable via `getActiveSubscriptions`.
- The attack requires only repeated calls with `msg.value == MAX_DEPOSIT_LIMIT` (100 ETH each), which passes the `>` check.
- No privileged access, no leaked keys, no governance majority required.

---

### Recommendation

Replace the per-transaction check in `addFunds` with a cumulative check:

```solidity
if (params.isPermanent &&
    status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

This mirrors the intent of the constant's NatSpec ("Maximum deposit limit for permanent subscriptions") and closes the bypass entirely.

---

### Proof of Concept

```solidity
// 1. Attacker finds any active permanent subscription (ID = 1)
// 2. Calls addFunds 10 times, each with exactly MAX_DEPOSIT_LIMIT (100 ETH)
for (uint i = 0; i < 10; i++) {
    scheduler.addFunds{value: 100 ether}(1);
    // Each call: msg.value (100 ether) > MAX_DEPOSIT_LIMIT (100 ether) → FALSE → no revert
    // status.balanceInWei grows: 100, 200, 300 ... 1000 ether
}
// 3. Subscription now holds 1000 ETH, 10× the intended cap
// 4. withdrawFunds reverts (permanent subscription) → ETH permanently locked
``` [5](#0-4) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```
