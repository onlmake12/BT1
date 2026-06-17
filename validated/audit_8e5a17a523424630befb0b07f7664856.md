### Title
`MAX_DEPOSIT_LIMIT` Bypass via Incremental `addFunds` Calls Permanently Locks ETH in Permanent Subscriptions — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `addFunds` function enforces `MAX_DEPOSIT_LIMIT` (100 ETH) only against the **single incoming `msg.value`**, not against the **cumulative `status.balanceInWei` after the deposit**. Because `addFunds` is permissionless and permanent subscriptions cannot withdraw funds, an attacker (or the manager themselves) can call `addFunds` repeatedly with amounts just at or below 100 ETH to accumulate a balance far exceeding the cap. The excess ETH is permanently locked in the contract with no recovery path.

---

### Finding Description

`SchedulerConstants.sol` defines:

```solidity
uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
``` [1](#0-0) 

The `addFunds` function in `Scheduler.sol` enforces this cap as follows:

```solidity
function addFunds(uint256 subscriptionId) external payable override {
    ...
    // Check deposit limit for permanent subscriptions
    if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
        revert SchedulerErrors.MaxDepositLimitExceeded();
    }

    status.balanceInWei += msg.value;
    ...
}
``` [2](#0-1) 

The check `msg.value > MAX_DEPOSIT_LIMIT` only validates the **current single deposit**, not the **post-deposit cumulative balance** (`status.balanceInWei + msg.value`). An attacker can call `addFunds` N times with `msg.value = MAX_DEPOSIT_LIMIT` (100 ETH each), accumulating `N × 100 ETH` in the subscription's balance — completely bypassing the cap.

The same flaw exists in `createSubscription`:

```solidity
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
``` [3](#0-2) 

The `withdrawFunds` function explicitly blocks withdrawals from permanent subscriptions:

```solidity
// Prevent withdrawals from permanent subscriptions
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [4](#0-3) 

This means any ETH deposited above the intended cap into a permanent subscription is **permanently irrecoverable**.

---

### Impact Explanation

1. **ETH permanently locked**: Any ETH deposited beyond `MAX_DEPOSIT_LIMIT` into a permanent subscription cannot be withdrawn. The `withdrawFunds` function unconditionally reverts for permanent subscriptions.
2. **Griefing attack**: Because `addFunds` is permissionless (any address can call it for any subscription ID), an attacker can grief a victim's permanent subscription by repeatedly calling `addFunds` with small amounts, locking the attacker's own ETH into the victim's subscription. The victim cannot undo this.
3. **Cap purpose defeated**: The `MAX_DEPOSIT_LIMIT` is intended to bound the ETH locked per permanent subscription. The bypass renders this control entirely ineffective.

---

### Likelihood Explanation

- `addFunds` is a public, permissionless function callable by any EOA or contract.
- No special role, key, or governance access is required.
- The attack requires only repeated ETH transfers, which is trivially executable on-chain.
- A subscription manager could also trigger this unintentionally by calling `addFunds` multiple times.

---

### Recommendation

Change the check in `addFunds` (and `createSubscription`) to validate the **post-deposit cumulative balance**, not just `msg.value`:

```solidity
// In addFunds:
if (params.isPermanent &&
    status.balanceInWei + msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
status.balanceInWei += msg.value;
```

```solidity
// In createSubscription:
if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}
// (createSubscription is fine as-is since balanceInWei starts at 0,
//  but addFunds must check the cumulative total)
```

---

### Proof of Concept

1. Alice creates a permanent subscription with `msg.value = 100 ETH` (exactly at `MAX_DEPOSIT_LIMIT`). This succeeds.
2. Bob (or Alice) calls `addFunds(subscriptionId)` with `msg.value = 100 ETH`. The check `100 ETH > 100 ETH` is **false**, so it passes. `status.balanceInWei` becomes 200 ETH.
3. Repeat step 2 N times. `status.balanceInWei` becomes `(N+1) × 100 ETH`.
4. Alice attempts `withdrawFunds` — it reverts unconditionally because the subscription is permanent.
5. All ETH above 100 ETH (and in fact all 100 ETH from each `addFunds` call) is permanently locked in the contract. [5](#0-4) [4](#0-3)

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
