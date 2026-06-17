### Title
`MAX_DEPOSIT_LIMIT` Bypass for Permanent Subscriptions via Repeated `addFunds` Calls — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `addFunds` function in `Scheduler.sol` checks only the single-transaction `msg.value` against `MAX_DEPOSIT_LIMIT`, not the cumulative subscription balance. An unprivileged user can call `addFunds` repeatedly with amounts at or just below `MAX_DEPOSIT_LIMIT` to accumulate a balance far exceeding the intended cap on permanent subscriptions, permanently locking more ETH than the protocol allows.

---

### Finding Description

`MAX_DEPOSIT_LIMIT` is defined as `100 ether` in `SchedulerConstants.sol` and is intended to cap the total funds deposited into a permanent subscription (which cannot be withdrawn).

In `createSubscription`, the check is:

```solidity
// File: Scheduler.sol, line 48
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

In `addFunds`, the same flawed pattern is repeated:

```solidity
// File: Scheduler.sol, lines 613–617
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
```

Both checks validate only the **incoming deposit amount** (`msg.value`) against the limit, not the **resulting cumulative balance** (`status.balanceInWei + msg.value`). Because `addFunds` can be called by any address (no `onlyManager` restriction), any caller can top up a permanent subscription repeatedly, each time depositing up to `MAX_DEPOSIT_LIMIT`, accumulating an unbounded total balance.

This is structurally identical to the Tigris Trade H-09 bug: a cap check uses a per-operation value instead of the proportional/cumulative value, allowing repeated partial operations to bypass the cap entirely.

---

### Impact Explanation

Permanent subscriptions explicitly prohibit fund withdrawal (`withdrawFunds` reverts with `CannotUpdatePermanentSubscription`). The `MAX_DEPOSIT_LIMIT` of 100 ETH exists precisely to prevent users from accidentally or maliciously locking more ETH than intended in an irrecoverable state.

By bypassing this limit:
- A user (or any third party, since `addFunds` has no access control) can lock an arbitrary multiple of 100 ETH permanently in the contract.
- The locked ETH is irrecoverable — neither the depositor nor the subscription manager can withdraw it.
- A malicious actor can grief a victim's permanent subscription by calling `addFunds` on their behalf, permanently locking the attacker's own ETH into the victim's subscription (denial-of-service via fund stuffing, forcing the keeper to service an over-funded subscription indefinitely).

---

### Likelihood Explanation

The entry path is fully unprivileged: `addFunds(uint256 subscriptionId)` is `external payable` with no access control. Any EOA or contract can call it for any active subscription ID. The only precondition is that the subscription exists and `isActive == true`. This is trivially satisfiable on any live deployment.

---

### Recommendation

Change both `createSubscription` and `addFunds` to check the **post-deposit cumulative balance** against `MAX_DEPOSIT_LIMIT`:

```solidity
// In createSubscription:
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}

// In addFunds — fix:
if (params.isPermanent && status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

Additionally, consider adding `onlyManager` access control to `addFunds` for permanent subscriptions to prevent third-party griefing.

---

### Proof of Concept

1. Alice creates a permanent subscription with `msg.value = 100 ETH`. The check `100 ETH > 100 ETH` is false, so it passes. `status.balanceInWei = 100 ETH`.
2. Alice (or anyone) calls `addFunds(subscriptionId)` with `msg.value = 100 ETH`. The check `100 ETH > 100 ETH` is false, so it passes. `status.balanceInWei = 200 ETH`.
3. Repeat step 2 N times. `status.balanceInWei = (N+1) * 100 ETH`.
4. Since `withdrawFunds` reverts for permanent subscriptions, all deposited ETH is permanently locked.
5. The `MAX_DEPOSIT_LIMIT` invariant is broken: the subscription holds an unbounded multiple of the intended cap.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L12-12)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-642)
```text
    function withdrawFunds(
        uint256 subscriptionId,
        uint256 amount
    ) external override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```
