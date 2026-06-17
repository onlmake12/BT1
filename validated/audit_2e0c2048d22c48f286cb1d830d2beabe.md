### Title
Permanent Subscription Funds Are Permanently Locked With No Emergency Recovery Mechanism - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler.sol` contract supports a `isPermanent` flag on subscriptions. Once set, this flag is irrevocable and unconditionally blocks all fund withdrawals. No admin, governance, or emergency path exists to recover ETH locked in permanent subscriptions. If the contract is deprecated, found to contain a bug, or needs migration, all ETH held in permanent subscriptions is permanently unrecoverable.

---

### Finding Description

In `Scheduler.sol`, the `withdrawFunds()` function contains an unconditional guard:

```solidity
// Prevent withdrawals from permanent subscriptions
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [1](#0-0) 

Similarly, `updateSubscription()` blocks all modifications — including deactivation and reverting `isPermanent` to `false` — once the flag is set:

```solidity
// Updates to permanent subscriptions are not allowed
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [2](#0-1) 

This means a permanent subscription can never be deactivated, and its balance can never be withdrawn by anyone — including the subscription manager. The `SchedulerGovernance.sol` contract provides no emergency withdrawal or fund-recovery function; it only exposes admin transfer and fee-setting operations: [3](#0-2) 

The `addFunds()` function still accepts ETH for permanent subscriptions (with a deposit cap), meaning additional ETH can be deposited into a permanent subscription but can never be retrieved: [4](#0-3) 

The test suite explicitly confirms this behavior — withdrawals from permanent subscriptions always revert: [5](#0-4) 

---

### Impact Explanation

Any ETH deposited into a permanent subscription is locked in the contract forever with no recovery path. If:
- A critical bug is discovered in `Scheduler.sol`,
- The contract needs to be deprecated and migrated to a new version,
- The Pyth oracle infrastructure changes in a way that makes the subscription useless,

…then all ETH in permanent subscriptions is permanently lost. There is no admin emergency withdrawal, no governance override, and no upgrade path that could recover these funds without a full contract replacement (which itself cannot move the locked ETH without a new withdrawal function).

---

### Likelihood Explanation

The `isPermanent` flag is a documented, user-facing feature. Any user who creates or upgrades a subscription to permanent — which is explicitly supported and tested — is exposed. The `MAX_DEPOSIT_LIMIT` allows up to a significant ETH amount per permanent subscription. As the Scheduler is used in production, permanent subscriptions will accumulate ETH with no recovery path. [6](#0-5) 

---

### Recommendation

Add an admin-only emergency withdrawal function in `SchedulerGovernance.sol` that can recover funds from permanent subscriptions in exceptional circumstances (contract deprecation, critical bug, etc.):

```solidity
function emergencyWithdraw(
    uint256 subscriptionId,
    address recipient
) external {
    _authorizeAdminAction();
    uint256 balance = _state.subscriptionStatuses[subscriptionId].balanceInWei;
    _state.subscriptionStatuses[subscriptionId].balanceInWei = 0;
    (bool sent, ) = recipient.call{value: balance}("");
    require(sent, "Transfer failed");
    emit EmergencyWithdrawal(subscriptionId, recipient, balance);
}
```

This mirrors the pattern recommended in the external report: provide a privileged escape hatch that bypasses normal withdrawal restrictions in emergencies.

---

### Proof of Concept

1. User calls `createSubscription({isPermanent: true, ...})` with `msg.value = 10 ether`.
2. User later calls `withdrawFunds(subscriptionId, 10 ether)`.
3. Transaction reverts with `CannotUpdatePermanentSubscription`.
4. User calls `updateSubscription(subscriptionId, {isPermanent: false, ...})`.
5. Transaction reverts with `CannotUpdatePermanentSubscription`.
6. No admin function exists to recover the 10 ETH.
7. The 10 ETH is permanently locked in `Scheduler.sol` with no recovery path. [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L47-50)
```text
        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-662)
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

        if (status.balanceInWei < amount) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // If subscription is active, ensure minimum balance is maintained
        if (params.isActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(params.priceIds.length)
            );
            if (status.balanceInWei - amount < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }

        status.balanceInWei -= amount;

        (bool sent, ) = msg.sender.call{value: amount}("");
        require(sent, "Failed to send funds");
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerGovernance.sol (L68-89)
```text
    function setSingleUpdateKeeperFeeInWei(uint128 newFee) external {
        _authorizeAdminAction();

        uint oldFee = _state.singleUpdateKeeperFeeInWei;
        _state.singleUpdateKeeperFeeInWei = newFee;

        emit SingleUpdateKeeperFeeSet(oldFee, newFee);
    }

    /**
     * @dev Set the minimum balance required per feed in a subscription.
     * Calls {_authorizeAdminAction}.
     * Emits a {MinimumBalancePerFeedSet} event.
     */
    function setMinimumBalancePerFeed(uint128 newMinimumBalance) external {
        _authorizeAdminAction();

        uint oldBalance = _state.minimumBalancePerFeed;
        _state.minimumBalancePerFeed = newMinimumBalance;

        emit MinimumBalancePerFeedSet(oldBalance, newMinimumBalance);
    }
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L898-911)
```text
        // Test 3: Cannot withdraw funds
        uint256 extraFunds = 1 ether;
        vm.deal(address(0x123), extraFunds);

        // Anyone can add funds (not just manager)
        vm.prank(address(0x123));
        scheduler.addFunds{value: extraFunds}(subscriptionId);

        vm.expectRevert(
            abi.encodeWithSelector(
                SchedulerErrors.CannotUpdatePermanentSubscription.selector
            )
        );
        scheduler.withdrawFunds(subscriptionId, 0.1 ether);
```
