### Title
`MAX_DEPOSIT_LIMIT` for Permanent Subscriptions Can Be Bypassed via Repeated `addFunds` Calls — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `addFunds` function in `Scheduler.sol` validates the deposit limit for permanent subscriptions by checking only the per-call `msg.value` against `MAX_DEPOSIT_LIMIT`, not the resulting cumulative `balanceInWei`. Any caller can bypass the 100 ETH cap by calling `addFunds` multiple times with amounts at or below the limit, permanently locking an unbounded amount of ETH in the contract.

### Finding Description
`SchedulerConstants.sol` defines `MAX_DEPOSIT_LIMIT = 100 ether` as the maximum deposit for permanent subscriptions. [1](#0-0) 

In `createSubscription`, the check is applied correctly against `msg.value` at creation time: [2](#0-1) 

However, in `addFunds`, the same pattern is repeated — checking only the incoming `msg.value`, not the resulting total balance: [3](#0-2) 

The check `msg.value > MAX_DEPOSIT_LIMIT` passes as long as each individual call sends ≤ 100 ETH. After the check, `status.balanceInWei += msg.value` accumulates the deposit unconditionally. There is no guard against the cumulative balance exceeding `MAX_DEPOSIT_LIMIT`.

`addFunds` has no access control — any address can call it for any active subscription: [4](#0-3) 

Permanent subscriptions cannot withdraw funds (`withdrawFunds` reverts for permanent subscriptions): [5](#0-4) 

So any ETH deposited beyond the intended cap is permanently and irrecoverably locked in the contract.

### Impact Explanation
**Medium.** The protocol invariant that permanent subscriptions are capped at `MAX_DEPOSIT_LIMIT` (100 ETH) is broken. Because `withdrawFunds` is blocked for permanent subscriptions, any ETH deposited above the cap is permanently locked. A third party (not the subscription manager) can call `addFunds` to grief a permanent subscription by locking arbitrary ETH in it, since `addFunds` is permissionless. The check gives a false sense of security — it appears to enforce a cap but does not.

### Likelihood Explanation
**Medium.** The bypass requires only repeated calls to a public function with no special privileges. The attacker must supply their own ETH, which limits direct financial incentive, but the griefing vector (locking ETH in someone else's permanent subscription) is costless to the victim and trivially executable.

### Recommendation
Change the validation in `addFunds` to check the resulting cumulative balance, not just the per-call `msg.value`:

```solidity
// In addFunds:
if (params.isPermanent && (status.balanceInWei + msg.value) > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
```

Apply the same fix to `createSubscription` for consistency (though at creation time `status.balanceInWei` is 0, so the current check is equivalent there).

### Proof of Concept

```solidity
// 1. Create a permanent subscription with MAX_DEPOSIT_LIMIT (100 ETH) — passes
scheduler.createSubscription{value: 100 ether}(permanentParams);
// status.balanceInWei == 100 ether

// 2. Call addFunds 9 more times with 100 ETH each — each call passes the check
//    because msg.value (100 ETH) is NOT > MAX_DEPOSIT_LIMIT (100 ETH)
for (uint i = 0; i < 9; i++) {
    scheduler.addFunds{value: 100 ether}(subscriptionId);
}
// status.balanceInWei == 1000 ether — 10x the intended cap
// Funds are permanently locked; withdrawFunds reverts for permanent subscriptions
```

Each iteration passes `msg.value > MAX_DEPOSIT_LIMIT` (line 613) because `100 ether > 100 ether` is false. The cumulative balance is never checked. [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-617)
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-641)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
```
