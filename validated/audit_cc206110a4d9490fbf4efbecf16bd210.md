### Title
Permanent Subscriptions Accept Unrestricted ETH Deposits With No Recovery Path — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `isPermanent` flag creates a one-way, irreversible state analogous to the Mooniswap shutdown-with-no-unpause pattern. Once a subscription is marked permanent, all fund withdrawal is permanently blocked. Critically, `addFunds` has no access control — any unprivileged address can deposit ETH into a permanent subscription — and those funds are permanently locked with no admin or governance recovery path.

---

### Finding Description

In `Scheduler.sol`, the `addFunds` function is callable by any address with no restriction: [1](#0-0) 

It accepts ETH for any active subscription, including permanent ones. The only guard for permanent subscriptions is a per-call deposit cap (`MAX_DEPOSIT_LIMIT = 100 ether`), not a withdrawal guarantee.

`withdrawFunds`, however, unconditionally reverts for any permanent subscription before any other logic: [2](#0-1) 

The `isPermanent` flag itself can never be unset — `updateSubscription` reverts immediately if `currentParams.isPermanent` is true, blocking every possible state change including deactivation: [3](#0-2) 

There is no admin-only rescue function, no governance override, and no emergency withdrawal path anywhere in the contract. The `SchedulerErrors` library confirms `CannotUpdatePermanentSubscription` is the terminal error for all mutation attempts: [4](#0-3) 

The `SubscriptionParams` struct confirms `isPermanent` is a plain boolean with no time-lock or reversibility mechanism: [5](#0-4) 

---

### Impact Explanation

Any ETH sent to a permanent subscription via `addFunds` is permanently locked in the contract. Neither the subscription manager, nor any admin, nor any governance action can recover it. The `MAX_DEPOSIT_LIMIT` of 100 ETH per call bounds individual transactions but does not prevent repeated calls: [6](#0-5) 

Additionally, the subscription manager's own initial deposit (made at `createSubscription`) is equally irrecoverable once `isPermanent` is set, with no emergency exit. If the tracked price feeds are later deprecated by Pyth governance, the subscription becomes permanently useless and the locked balance is unrecoverable.

---

### Likelihood Explanation

The `addFunds` function is intentionally public and undocumented regarding the permanent-lock consequence for permanent subscriptions. A third party (e.g., a protocol integrator, a keeper, or a well-meaning user) can call it without realizing the ETH is unrecoverable. The test suite itself demonstrates this is an expected call pattern: [7](#0-6) 

The subscription manager can also trigger the lock on their own funds by setting `isPermanent = true` and later discovering the price feeds they subscribed to are no longer useful, with no recourse.

---

### Recommendation

1. Add an admin-only (or governance-controlled) emergency withdrawal function that can rescue funds from permanent subscriptions in exceptional circumstances.
2. Restrict `addFunds` for permanent subscriptions to `onlyManager`, or at minimum emit a clear revert if the subscription is permanent and the caller is not the manager, to prevent accidental permanent fund loss by third parties.
3. Consider a time-locked or governance-gated mechanism to un-permanent a subscription, mirroring the "unpause" pattern recommended in the Mooniswap report.

---

### Proof of Concept

```
1. Alice creates a permanent subscription:
   scheduler.createSubscription{value: minimumBalance}(params_with_isPermanent_true)
   → subscriptionId = 1

2. Bob (unprivileged) calls:
   scheduler.addFunds{value: 50 ether}(1)
   → Succeeds. Bob's 50 ETH is now in the contract.

3. Bob tries to recover his ETH — impossible:
   scheduler.withdrawFunds(1, 50 ether)
   → Reverts: CannotUpdatePermanentSubscription

4. Alice (manager) tries to deactivate and recover:
   scheduler.updateSubscription(1, params_with_isActive_false)
   → Reverts: CannotUpdatePermanentSubscription

5. No admin function exists to rescue the 50 ETH. Funds are permanently locked.
``` [1](#0-0) [8](#0-7)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerErrors.sol (L16-16)
```text
    error CannotUpdatePermanentSubscription();
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L14-14)
```text
        bool isPermanent; // Whether the subscription can be updated
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L12-12)
```text
    uint256 public constant MAX_DEPOSIT_LIMIT = 100 ether;
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L902-904)
```text
        // Anyone can add funds (not just manager)
        vm.prank(address(0x123));
        scheduler.addFunds{value: extraFunds}(subscriptionId);
```
