### Title
`getMinimumBalance` Understates True Required Balance, Causing `updatePriceFeeds` to Revert — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getMinimumBalance` returns a value that is enforced as the floor for `createSubscription`, `addFunds`, and `updateSubscription`, but it does not account for the Pyth oracle fee (`pythFee`) charged inside `updatePriceFeeds`. A subscription funded at exactly `getMinimumBalance()` can therefore have every `updatePriceFeeds` call revert with `InsufficientBalance`, leaving price feeds permanently stale.

---

### Finding Description

`getMinimumBalance` is defined as:

```solidity
function getMinimumBalance(uint8 numPriceFeeds)
    external view override returns (uint256 minimumBalanceInWei)
{
    // TODO: Consider adding a base minimum balance independent of feed count
    return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
}
```

It returns only `numPriceFeeds × minimumBalancePerFeed` — a static, admin-configured value that scales linearly with feed count.

`updatePriceFeeds` deducts **three** separate costs from the subscription balance:

1. **`pythFee`** — `pyth.getUpdateFee(updateData)`, charged before any other logic:
   ```solidity
   uint256 pythFee = pyth.getUpdateFee(updateData);
   if (status.balanceInWei < pythFee) revert SchedulerErrors.InsufficientBalance();
   status.balanceInWei -= pythFee;   // deducted here
   ```

2. **`gasCost`** — dynamic, `(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice`

3. **`keeperSpecificFee`** — `singleUpdateKeeperFeeInWei × numPriceIds`

The second and third are checked together in `_processFeesAndPayKeeper` against the **already-reduced** balance (after `pythFee` was removed):
```solidity
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
if (status.balanceInWei < totalKeeperFee) revert SchedulerErrors.InsufficientBalance();
```

`getMinimumBalance` includes **none** of `pythFee` and **none** of `gasCost`. The `minimumBalancePerFeed` parameter has no on-chain relationship to either of those costs; the `TODO` comment in `getMinimumBalance` itself acknowledges the gap.

Because `addFunds` enforces `status.balanceInWei >= getMinimumBalance()` after the deposit, a user who tops up to exactly the documented minimum will still have every subsequent `updatePriceFeeds` call revert the moment `pythFee > 0` (which is always true in production).

---

### Impact Explanation

- Price feeds for subscriptions funded at the documented minimum balance are never updated.
- DeFi protocols that are whitelisted readers of such subscriptions receive stale prices, which can lead to incorrect liquidations, mispriced derivatives, or other financial harm.
- The subscription manager has no on-chain signal that the minimum balance is insufficient; `addFunds` accepts the deposit and emits no warning.

---

### Likelihood Explanation

- `getMinimumBalance` is the only public view function documenting the required balance floor. The SDK README and `IScheduler` NatSpec both direct users to call it before funding.
- The test helper `addTestSubscription` funds at exactly `getMinimumBalance()`, demonstrating the intended usage pattern.
- In production, `pyth.getUpdateFee(updateData)` is non-zero (it equals `singleUpdateFeeInWei × numUpdates`). Any subscription funded at the minimum will fail on the first keeper call.
- Gas price spikes compound the issue: even if `pythFee` is covered, a high `tx.gasprice` can push `gasCost` above the remaining balance.

---

### Recommendation

`getMinimumBalance` should incorporate an estimate of `pythFee` and a gas-cost buffer:

```solidity
function getMinimumBalance(uint8 numPriceFeeds)
    external view override returns (uint256 minimumBalanceInWei)
{
    uint256 feedFloor = uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
    // Add one update's worth of Pyth fee (worst-case: one update data item per feed)
    uint256 estimatedPythFee = IPyth(_state.pyth).getSingleUpdateFeeInWei() * numPriceFeeds;
    // Add a gas-cost buffer at a reference gas price (e.g., stored by admin)
    uint256 gasBuffer = (GAS_OVERHEAD + ESTIMATED_UPDATE_GAS) * _state.referenceGasPriceInWei;
    return feedFloor + estimatedPythFee + gasBuffer;
}
```

Alternatively, document clearly that `minimumBalancePerFeed` **must** be set by the admin to cover `pythFee + gasCost + keeperSpecificFee` per update, and add an on-chain assertion in `setMinimumBalancePerFeed` that enforces this invariant.

---

### Proof of Concept

```solidity
function testMinBalanceDoesNotCoverPythFee() public {
    // Deploy with minimumBalancePerFeed = keeperFee only (no pythFee margin)
    uint128 minBalancePerFeed = 10 ** 14; // 0.0001 ETH (only covers keeperFee)
    uint128 keeperFee        = 10 ** 14;
    // ... initialize scheduler with these params ...

    // Create subscription funded at exactly getMinimumBalance()
    SchedulerStructs.SubscriptionParams memory params =
        createDefaultSubscriptionParams(2, address(reader));
    uint256 minBalance = scheduler.getMinimumBalance(2); // = 2 * 0.0001 ETH = 0.0002 ETH

    uint256 subId = scheduler.createSubscription{value: minBalance}(params);

    // Mock Pyth to charge a non-zero pythFee (e.g., 0.001 ETH per update)
    // pythFee > minBalance → updatePriceFeeds reverts immediately
    vm.prank(pusher);
    vm.expectRevert(SchedulerErrors.InsufficientBalance.selector);
    scheduler.updatePriceFeeds(subId, updateData); // reverts: pythFee > balanceInWei
}
```

The `getMinimumBalance` check in `addFunds` accepted the deposit, but `updatePriceFeeds` reverts because `pythFee` was never included in the minimum balance calculation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L292-306)
```text
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L602-627)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L734-739)
```text
    function getMinimumBalance(
        uint8 numPriceFeeds
    ) external view override returns (uint256 minimumBalanceInWei) {
        // TODO: Consider adding a base minimum balance independent of feed count
        return uint256(numPriceFeeds) * this.getMinimumBalancePerFeed();
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L844-854)
```text
    ) internal {
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L19-22)
```text
        /// Fee in wei charged to subscribers per single update triggered by a keeper
        uint128 singleUpdateKeeperFeeInWei;
        /// Minimum balance required per price feed in a subscription
        uint128 minimumBalancePerFeed;
```
