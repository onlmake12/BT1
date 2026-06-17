### Title
`Scheduler.addFunds()` Deposit Limit Check Ignores Existing Balance, Allowing Permanent Subscriptions to Exceed `MAX_DEPOSIT_LIMIT` — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.addFunds()` checks only whether the incoming `msg.value` exceeds `MAX_DEPOSIT_LIMIT`, without accounting for the subscription's existing balance. Because `addFunds` has no access control, any caller can make repeated deposits each below the limit, accumulating a total balance far beyond `MAX_DEPOSIT_LIMIT` in a permanent subscription. Since permanent subscriptions cannot withdraw funds, this ETH is permanently locked.

---

### Finding Description

`Scheduler.addFunds()` enforces a deposit cap for permanent subscriptions:

```solidity
// Check deposit limit for permanent subscriptions
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}

status.balanceInWei += msg.value;
```

The check validates only `msg.value` in isolation, not `status.balanceInWei + msg.value`. `MAX_DEPOSIT_LIMIT` is 100 ETH. A caller can invoke `addFunds` N times, each with `msg.value = MAX_DEPOSIT_LIMIT`, accumulating `N × 100 ETH` in the subscription's balance — all while every individual check passes.

The analogous correct check at creation time is also flawed in the same way for `createSubscription`:

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

Here it is harmless because `balanceInWei` starts at zero. But in `addFunds`, the existing balance is non-zero and must be included in the comparison.

`addFunds` has no access control — the NatSpec and tests explicitly confirm "Anyone can add funds":

```solidity
function addFunds(uint256 subscriptionId) external payable override {
```

---

### Impact Explanation

1. **Deposit cap bypass by the manager:** The subscription manager can deposit an arbitrary total amount into their own permanent subscription by splitting deposits into chunks ≤ 100 ETH each. The `MAX_DEPOSIT_LIMIT` invariant is completely ineffective.

2. **Griefing via forced permanent lock:** Because `addFunds` is permissionless, any third party can call it on any permanent subscription. Permanent subscriptions cannot withdraw funds (`withdrawFunds` reverts with `CannotUpdatePermanentSubscription`). An attacker can permanently lock their own ETH into a victim's permanent subscription, or inflate the balance of a protocol-owned permanent subscription beyond the intended cap, violating the protocol's accounting invariant.

The combined effect is that the `MAX_DEPOSIT_LIMIT` protection for permanent subscriptions provides zero security guarantee.

---

### Likelihood Explanation

High. The entry path requires no privilege — any EOA can call `addFunds` on any active permanent subscription. The bypass requires only multiple sequential transactions, each with `msg.value ≤ MAX_DEPOSIT_LIMIT`. No special knowledge, leaked keys, or governance access is needed.

---

### Recommendation

Change the check in `addFunds` to validate the **post-deposit cumulative balance** against `MAX_DEPOSIT_LIMIT`:

```solidity
// Check deposit limit for permanent subscriptions
if (params.isPermanent && status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

This mirrors the fix described in the external report: account for the existing value when checking against the maximum.

---

### Proof of Concept

1. Deploy `Scheduler` with `MAX_DEPOSIT_LIMIT = 100 ether`.
2. Create a permanent subscription with `msg.value = 100 ether` — succeeds (at the limit).
3. Call `addFunds{value: 100 ether}(subscriptionId)` — `msg.value (100e18) > MAX_DEPOSIT_LIMIT (100e18)` is **false** (not strictly greater), so the check passes. Balance is now 200 ETH.
4. Repeat step 3 — balance grows to 300 ETH, 400 ETH, etc.
5. Alternatively, call `addFunds{value: 1 ether}(subscriptionId)` 101 times — each call passes since `1 ether ≤ MAX_DEPOSIT_LIMIT`, total balance reaches 201 ETH.
6. Confirm `status.balanceInWei > MAX_DEPOSIT_LIMIT` via `getSubscription`.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-618)
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

```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L11-12)
```text
    /// Maximum deposit limit for permanent subscriptions in wei
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```
