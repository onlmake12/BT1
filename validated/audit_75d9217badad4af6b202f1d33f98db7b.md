### Title
Permanent Subscription Balance Deadlock Due to `MAX_DEPOSIT_LIMIT` and Minimum Balance Constraint Interaction — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, the `addFunds` function enforces two mutually exclusive constraints for **permanent subscriptions**: (1) `msg.value` must not exceed `MAX_DEPOSIT_LIMIT`, and (2) after adding funds, the resulting balance must be at or above `minimumBalance`. When a permanent subscription's balance falls below `minimumBalance` by more than `MAX_DEPOSIT_LIMIT`, no valid deposit amount can satisfy both constraints simultaneously. Since permanent subscriptions cannot be deactivated or have funds withdrawn, the subscription enters a permanent deadlock where it can never be refunded — an exact structural analog to the Notional partial-liquidation deadlock.

---

### Finding Description

The `addFunds` function applies two sequential checks for permanent subscriptions:

```solidity
// Check 1: per-transaction deposit cap for permanent subscriptions
if (params.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
    revert SchedulerErrors.MaxDepositLimitExceeded();
}

status.balanceInWei += msg.value;

// Check 2: post-deposit minimum balance enforcement
if (params.isActive) {
    uint256 minimumBalance = this.getMinimumBalance(
        uint8(params.priceIds.length)
    );
    if (status.balanceInWei < minimumBalance) {
        revert SchedulerErrors.InsufficientBalance();
    }
}
``` [1](#0-0) 

When `minimumBalance − currentBalance > MAX_DEPOSIT_LIMIT`:

- Any `msg.value ≤ MAX_DEPOSIT_LIMIT` → post-deposit balance still below minimum → `InsufficientBalance` revert.
- Any `msg.value > MAX_DEPOSIT_LIMIT` → `MaxDepositLimitExceeded` revert.
- **No valid `msg.value` exists.**

The two escape hatches that would break the deadlock are both blocked for permanent subscriptions:

**`withdrawFunds` is blocked:**
```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [2](#0-1) 

**`updateSubscription` is blocked** (funds added via `msg.value` are reverted along with the transaction):
```solidity
currentStatus.balanceInWei += msg.value;
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [3](#0-2) 

The balance of a permanent subscription is drained by keeper calls to `updatePriceFeeds`, which deducts `singleUpdateKeeperFeeInWei` per update. The existing test confirms that repeated updates can drain the balance well below `minimumBalance` without any guard stopping them:

```solidity
// Send multiple price updates to drain the balance below minimum
for (uint i = 0; i < 5; i++) { ... scheduler.updatePriceFeeds(subscriptionId, updateData); }
assertTrue(statusAfterUpdates.balanceInWei < minimumBalance, ...);
``` [4](#0-3) 

The same test also confirms that `addFunds` reverts when the deposit would not reach minimum: [5](#0-4) 

---

### Impact Explanation

A permanent subscription whose balance has been drained below `minimumBalance` by more than `MAX_DEPOSIT_LIMIT` is permanently irrecoverable:

- The manager cannot top up the subscription (deadlock above).
- The subscription remains active (permanent subscriptions cannot be deactivated), so keepers continue draining the remaining balance to zero.
- Once balance reaches zero, keepers can no longer be paid and price feed updates cease entirely.
- Any downstream protocol relying on the subscription for price data loses access to fresh prices, potentially causing stale-price failures, incorrect liquidations, or protocol insolvency.

The `IScheduler` interface documents this risk implicitly: "A minimum balance must be maintained for active subscriptions. To withdraw past the minimum balance limit, deactivate the subscription first." — but permanent subscriptions have no deactivation path. [6](#0-5) 

---

### Likelihood Explanation

The deadlock condition (`minimumBalance − currentBalance > MAX_DEPOSIT_LIMIT`) is reachable under normal operation:

1. A subscription manager creates a permanent subscription with many price feeds (e.g., `MAX_PRICE_IDS_PER_SUBSCRIPTION` feeds), resulting in a large `minimumBalance = numFeeds × minimumBalancePerFeed`.
2. The subscription is funded just above `minimumBalance` at creation.
3. Keepers (permissionless, incentivized by fees) call `updatePriceFeeds` repeatedly, draining the balance far below `minimumBalance`.
4. The gap `minimumBalance − currentBalance` grows with each update and can exceed `MAX_DEPOSIT_LIMIT`.
5. The manager discovers the deadlock and cannot rescue the subscription.

The attacker-controlled entry path is entirely permissionless: any address can call `updatePriceFeeds` on any active subscription, accelerating the drain.

---

### Recommendation

Remove the minimum balance post-deposit check from `addFunds` for permanent subscriptions, since they cannot be deactivated and the check serves no protective purpose there. Alternatively, allow deposits that bring the balance to exactly `minimumBalance` even if the gap exceeds `MAX_DEPOSIT_LIMIT` by splitting the top-up across multiple transactions (i.e., remove the `InsufficientBalance` revert when the deposit is the maximum allowed and still falls short):

```solidity
// Only enforce minimum balance if the deposit is not capped by MAX_DEPOSIT_LIMIT
if (params.isActive) {
    uint256 minimumBalance = this.getMinimumBalance(uint8(params.priceIds.length));
    bool cappedByLimit = params.isPermanent && msg.value == MAX_DEPOSIT_LIMIT;
    if (!cappedByLimit && status.balanceInWei < minimumBalance) {
        revert SchedulerErrors.InsufficientBalance();
    }
}
```

Or, more cleanly, remove the minimum balance enforcement from `addFunds` entirely and only enforce it at the point where keepers are paid (in `updatePriceFeeds`).

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` with `minimumBalancePerFeed = 1 ether` and `MAX_DEPOSIT_LIMIT = 1 ether`.
2. Create a permanent subscription with 5 price feeds: `minimumBalance = 5 ether`. Fund with exactly `5 ether`.
3. Call `updatePriceFeeds` 5 times (each costs `singleUpdateKeeperFeeInWei`). After enough updates, `balanceInWei` falls to, say, `3.5 ether` — a gap of `1.5 ether > MAX_DEPOSIT_LIMIT (1 ether)`.
4. Attempt `addFunds{value: 1 ether}`: balance becomes `4.5 ether < 5 ether` → reverts `InsufficientBalance`.
5. Attempt `addFunds{value: 1.5 ether}`: `1.5 ether > MAX_DEPOSIT_LIMIT` → reverts `MaxDepositLimitExceeded`.
6. Attempt `updateSubscription{value: 1.5 ether}(id, params)`: reverts `CannotUpdatePermanentSubscription` (funds not saved).
7. Attempt `withdrawFunds(id, 1)`: reverts `CannotUpdatePermanentSubscription`.
8. **Deadlock confirmed.** The subscription can never be refunded and will drain to zero.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L87-92)
```text
        currentStatus.balanceInWei += msg.value;

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L727-758)
```text
        // Send multiple price updates to drain the balance below minimum
        for (uint i = 0; i < 5; i++) {
            // Advance time to satisfy heartbeat criteria
            vm.warp(block.timestamp + 60);

            // Create price feeds with current timestamp
            uint64 publishTime = SafeCast.toUint64(block.timestamp);
            PythStructs.PriceFeed[] memory priceFeeds;
            uint64[] memory slots;
            (priceFeeds, slots) = createMockPriceFeedsWithSlots(
                publishTime,
                params.priceIds.length
            );

            // Mock Pyth response
            mockParsePriceFeedUpdatesWithSlotsStrict(pyth, priceFeeds, slots);
            bytes[] memory updateData = createMockUpdateData(priceFeeds);

            // Perform update
            vm.prank(pusher);
            scheduler.updatePriceFeeds(subscriptionId, updateData);
        }

        // Verify balance is now below minimum
        (
            ,
            SchedulerStructs.SubscriptionStatus memory statusAfterUpdates
        ) = scheduler.getSubscription(subscriptionId);
        assertTrue(
            statusAfterUpdates.balanceInWei < minimumBalance,
            "Balance should be below minimum after updates"
        );
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L760-768)
```text
        // Try to add funds that would still leave balance below minimum
        // Expect a revert with InsufficientBalance
        uint256 insufficientFunds = minimumBalance -
            statusAfterUpdates.balanceInWei -
            1;
        vm.expectRevert(
            abi.encodeWithSelector(SchedulerErrors.InsufficientBalance.selector)
        );
        scheduler.addFunds{value: insufficientFunds}(subscriptionId);
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L111-116)
```text
    /// @notice Withdraws funds from a subscription's balance.
    /// @dev A minimum balance must be maintained for active subscriptions. To withdraw past
    /// the minimum balance limit, deactivate the subscription first.
    /// @param subscriptionId The ID of the subscription
    /// @param amount The amount to withdraw
    function withdrawFunds(uint256 subscriptionId, uint256 amount) external;
```
