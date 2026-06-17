### Title
Subscription Manager Can Frontrun Keeper's `updatePriceFeeds` to Cause Repeated Reverts via `withdrawFunds` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

A subscription manager can monitor the mempool for a keeper's pending `updatePriceFeeds` transaction and frontrun it with `withdrawFunds`, reducing `status.balanceInWei` to exactly `minimumBalance`. If `minimumBalance` is insufficient to cover `pythFee + totalKeeperFee` (which is dynamic and gas-price-dependent), the keeper's transaction reverts. The manager can repeat this indefinitely, permanently preventing price updates for the subscription while wasting keeper gas.

---

### Finding Description

`Scheduler.updatePriceFeeds` performs two sequential balance checks against the same `status.balanceInWei` storage slot:

**Check 1** — before paying Pyth: [1](#0-0) 

**Deduction** — Pyth fee is deducted and ETH is forwarded: [2](#0-1) 

**Check 2** — inside `_processFeesAndPayKeeper`, after gas is consumed: [3](#0-2) 

The `totalKeeperFee` is computed dynamically as `(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice + singleUpdateKeeperFeeInWei * numPriceIds`, making it sensitive to gas price at execution time. [4](#0-3) 

Meanwhile, `withdrawFunds` (restricted to `onlyManager`) allows the subscription manager to reduce `status.balanceInWei` down to `minimumBalance = numPriceFeeds * minimumBalancePerFeed`: [5](#0-4) 

The `minimumBalance` is a static admin-configured value that does **not** account for dynamic gas prices. During gas price spikes, `minimumBalance` can be less than `pythFee + totalKeeperFee`.

**Attack flow:**
1. Manager creates a subscription with balance slightly above `minimumBalance`.
2. A keeper observes the subscription is due for an update and submits `updatePriceFeeds`.
3. Manager sees the keeper's pending transaction in the mempool and frontruns it with `withdrawFunds(subscriptionId, balance - minimumBalance)`, reducing balance to exactly `minimumBalance`.
4. The keeper's `updatePriceFeeds` passes Check 1 (if `minimumBalance >= pythFee`), deducts `pythFee`, then reverts at Check 2 because `minimumBalance - pythFee < totalKeeperFee`.
5. The entire transaction reverts (including the Pyth fee deduction), but the keeper has spent gas.
6. The manager repeats step 3 for every subsequent keeper attempt.

---

### Impact Explanation

- Keepers repeatedly waste gas on reverted `updatePriceFeeds` transactions with no compensation.
- The subscription's stored prices become permanently stale, breaking any reader protocol that depends on `getPricesNoOlderThan` or `getEmaPricesNoOlderThan`.
- The subscription remains listed as "active" in `getActiveSubscriptions`, so keepers continue to attempt updates and continue to lose gas.
- The manager pays only the gas for `withdrawFunds` (a cheap write), while the keeper pays the full gas cost of a complex `updatePriceFeeds` execution that reverts late.

---

### Likelihood Explanation

- The attack requires only the subscription manager — an unprivileged role with no special trust assumptions — to call `withdrawFunds`, which is a standard user-facing function.
- Motivation exists: a manager may wish to keep a subscription "active" (so readers can still call `getPricesUnsafe` on stale data) while avoiding keeper fee payments.
- On chains with public mempools (all current EVM deployments), frontrunning is straightforward.
- The condition `minimumBalance < pythFee + totalKeeperFee` is realistic during any gas price spike, since `minimumBalancePerFeed` is a static admin parameter that cannot track real-time gas prices.

---

### Recommendation

In `withdrawFunds`, enforce that the post-withdrawal balance is sufficient not just for `minimumBalance` but also for at least one full update cycle (i.e., `pythFee + estimatedKeeperFee`). Alternatively, when `updatePriceFeeds` detects insufficient balance for the keeper fee at Check 2, instead of reverting the entire transaction, cap the keeper payment to the available balance and proceed — ensuring the keeper is partially compensated and the price update is not lost.

---

### Proof of Concept

1. Manager creates a subscription with 2 price feeds. `minimumBalance = 2 * minimumBalancePerFeed`. Manager deposits `minimumBalance + 1 wei`.
2. Gas price spikes such that `totalKeeperFee > minimumBalance - pythFee`.
3. Keeper submits `updatePriceFeeds(subscriptionId, updateData)`.
4. Manager frontruns with `withdrawFunds(subscriptionId, 1 wei)`, reducing balance to `minimumBalance`.
5. Keeper's transaction executes:
   - Line 295: `minimumBalance >= pythFee` → passes.
   - Line 305: `balanceInWei = minimumBalance - pythFee`.
   - Line 852: `minimumBalance - pythFee < totalKeeperFee` → **reverts**.
6. Keeper loses gas. Manager re-deposits 1 wei and repeats from step 3.
7. Subscription prices are never updated; readers receive stale data indefinitely.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L294-297)
```text
        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-311)
```text
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L845-849)
```text
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L851-854)
```text
        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```
