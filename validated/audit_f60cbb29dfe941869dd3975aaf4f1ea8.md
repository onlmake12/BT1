### Title
Insufficient Balance Pre-Check in `updatePriceFeeds()` Causes Repeated Keeper Reverts — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
`Scheduler.updatePriceFeeds()` performs an early balance check that only verifies the subscription has enough funds to cover the Pyth oracle fee (`pythFee`), but the function also requires sufficient balance to pay the keeper fee at the end. When the subscription balance falls into the range `[pythFee, pythFee + totalKeeperFee)`, the early check passes, the Pyth fee is deducted, and the transaction reverts at `_processFeesAndPayKeeper()`. Any keeper calling this function in that balance range will repeatedly waste gas on reverting transactions.

### Finding Description

In `updatePriceFeeds()`, the balance check at line 295 only guards against the Pyth oracle fee:

```solidity
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [1](#0-0) 

Immediately after, the Pyth fee is deducted from the balance:

```solidity
status.balanceInWei -= pythFee;
``` [2](#0-1) 

At the very end of the function, `_processFeesAndPayKeeper()` is called, which checks the **remaining** balance against the full keeper fee (gas cost + per-feed fee):

```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [3](#0-2) 

The keeper fee is computed dynamically as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [4](#0-3) 

The two conditions are logically disconnected: the entry guard checks only `balance >= pythFee`, but successful execution requires `balance >= pythFee + totalKeeperFee`. This is the direct analog of the `||` vs `&&` bug in the reference report.

Tests confirm that subscription balances naturally drain below the minimum balance after repeated updates: [5](#0-4) 

### Impact Explanation

When a subscription balance is in the range `[pythFee, pythFee + totalKeeperFee)`:
1. The early check at line 295 passes.
2. The Pyth fee is deducted from `status.balanceInWei` in memory.
3. The entire expensive execution path runs (VAA parsing, slot verification, deviation/heartbeat validation).
4. `_processFeesAndPayKeeper()` reverts because the remaining balance is insufficient.
5. The entire transaction reverts — the keeper loses all gas spent.

Since the subscription remains active and the balance does not change (revert rolls back state), a keeper will repeat this reverting call on every subsequent block until the balance is topped up or the subscription is deactivated. This is a direct gas-drain on keepers and a liveness failure for the subscription's price feeds.

### Likelihood Explanation

Subscription balances drain naturally with every successful `updatePriceFeeds()` call. The `minimumBalance` enforced by `addFunds()` and `withdrawFunds()` does not guarantee the balance stays above `pythFee + totalKeeperFee` — it only enforces a static floor based on `minimumBalancePerFeed * numFeeds`. As gas prices fluctuate, `totalKeeperFee` can exceed the remaining balance even when the static minimum is met. Any unprivileged keeper can trigger this path by calling `updatePriceFeeds()` on a subscription in this balance range.

### Recommendation

Add a combined pre-check at the top of `updatePriceFeeds()` that accounts for both the Pyth fee and an estimated keeper fee before proceeding:

```solidity
uint256 estimatedKeeperFee = (uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length)
    + (GAS_OVERHEAD * tx.gasprice);
if (status.balanceInWei < pythFee + estimatedKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This mirrors the recommendation in the reference report: bind both required conditions with `&&` (i.e., check both fees together) before executing any state-changing logic.

### Proof of Concept

1. Create a subscription with 2 price feeds and fund it to exactly `pythFee + totalKeeperFee - 1 wei`.
2. Call `updatePriceFeeds(subscriptionId, updateData)` as an unprivileged keeper.
3. Observe: the check at line 295 passes (`balance >= pythFee`), the Pyth fee is deducted, all validation logic runs, and the transaction reverts at `_processFeesAndPayKeeper()` with `InsufficientBalance`.
4. The subscription balance is unchanged (revert). Repeat step 2 on the next block — same result every time.
5. The keeper loses gas on every call; the subscription's price feeds are never updated.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L294-297)
```text
        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-305)
```text
        status.balanceInWei -= pythFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L846-849)
```text
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L852-854)
```text
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L750-758)
```text
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
