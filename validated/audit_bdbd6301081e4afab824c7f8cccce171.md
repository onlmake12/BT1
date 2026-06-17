### Title
Keeper Can Drain Subscription Balance via Inflated `tx.gasprice` in `_processFeesAndPayKeeper` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `Scheduler` contract's `_processFeesAndPayKeeper` function calculates the keeper reimbursement using `tx.gasprice`, which is fully attacker-controlled. Any unprivileged address acting as a keeper can call `updatePriceFeeds` with an arbitrarily inflated gas price to drain a subscription's entire ETH balance in a single transaction, far exceeding the actual cost of the update.

---

### Finding Description

In `Scheduler.sol`, `_processFeesAndPayKeeper` computes the keeper fee as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
```

`GAS_OVERHEAD` is a fixed constant of `30_000` gas units. The entire `totalKeeperFee` is paid out to `msg.sender` and deducted from `status.balanceInWei`.

There is **no cap on `tx.gasprice`** and **no access control on `updatePriceFeeds`** — any address may call it. A keeper who submits the transaction with `tx.gasprice = X` receives `(gasUsed + 30_000) * X` from the subscription. By setting `X` to an extreme value (e.g., `type(uint256).max / gasUsed`), the keeper can extract the subscription's entire balance in one call, limited only by `status.balanceInWei`.

The profit motive is concrete: the keeper pays `gasUsed_total * X` to the network but receives `(gasUsed_measured + GAS_OVERHEAD) * X` from the subscription. Since `GAS_OVERHEAD` (30,000) is intended to cover base tx overhead but may overestimate it, the keeper nets `(GAS_OVERHEAD - actual_overhead) * X` per update — a profit that scales linearly with the chosen gas price.

---

### Impact Explanation

- **Subscription balance drain**: A single `updatePriceFeeds` call at an inflated gas price can consume the subscription's entire `balanceInWei`, depriving the subscription of funds for future legitimate updates and causing service disruption for the subscription owner.
- **Keeper profit extraction**: The keeper extracts `GAS_OVERHEAD * tx.gasprice` above their actual network cost per update. At extreme gas prices this is unbounded.
- **No recovery path**: Once the balance is drained below `getMinimumBalance`, the subscription is effectively dead until the owner re-funds it. For permanent subscriptions (which cannot be updated), the damage is irreversible within the deposit cap.

---

### Likelihood Explanation

`updatePriceFeeds` has no access control — any EOA or contract can call it. The attacker only needs to submit a valid price update (obtainable from the public Pyth data network) with an inflated `tx.gasprice`. On EIP-1559 chains, `tx.gasprice` equals `min(maxFeePerGas, baseFee + maxPriorityFeePerGas)`, so the attacker sets `maxFeePerGas` and `maxPriorityFeePerGas` to extreme values. The only cost to the attacker is the actual gas consumed at the inflated price, which they recover from the subscription's balance. This is a straightforward, low-skill attack with a direct financial incentive.

---

### Recommendation

1. **Cap `tx.gasprice` in the fee calculation**: Introduce a configurable `maxGasPriceInWei` parameter and clamp the effective gas price:
   ```solidity
   uint256 effectiveGasPrice = maxGasPriceInWei > 0
       ? Math.min(tx.gasprice, maxGasPriceInWei)
       : tx.gasprice;
   uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
   ```
2. **Use a time-weighted or oracle-based gas price** instead of `tx.gasprice` directly, similar to how Fortuna's `adjust_fee_if_necessary` estimates gas cost from chain state rather than trusting the caller's gas price.
3. **Add a maximum keeper fee per update** as an absolute ceiling on `totalKeeperFee` regardless of gas price.

---

### Proof of Concept

1. Alice creates a subscription with 2 price feeds and deposits `1 ether` as balance.
2. Eve (attacker) observes the subscription on-chain via `getActiveSubscriptions`.
3. Eve fetches valid Pyth update data for the subscribed price IDs from the public Pyth network.
4. Eve calls `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 1e18 wei` (1 ETH per gas unit).
5. Inside `_processFeesAndPayKeeper`:
   - `startGas - gasleft()` ≈ 200,000 gas (actual execution cost)
   - `GAS_OVERHEAD` = 30,000
   - `gasCost = (200,000 + 30,000) * 1e18 = 2.3e23 wei`
   - This exceeds `status.balanceInWei = 1e18`, so the check `status.balanceInWei < totalKeeperFee` triggers `InsufficientBalance`.
6. Eve adjusts: sets `tx.gasprice` such that `(gasUsed + 30_000) * tx.gasprice ≈ status.balanceInWei`. For example, with `gasUsed ≈ 200,000` and balance `= 1 ether`, Eve sets `tx.gasprice ≈ 1e18 / 230_000 ≈ 4.3e12 wei` (4,300 gwei — ~140x normal mainnet price).
7. Eve receives nearly the entire `1 ether` subscription balance in a single call, paying only `~200,000 * 4.3e12 ≈ 0.86 ether` in actual gas to the network, netting a profit of `GAS_OVERHEAD * tx.gasprice ≈ 30,000 * 4.3e12 ≈ 0.13 ether`.
8. Alice's subscription balance is fully drained; no further price updates occur. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-348)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
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
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );

        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }

        // Verify that update conditions are met, and that the timestamp
        // is more recent than latest stored update's. Reverts if not.
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-864)
```text
    function _processFeesAndPayKeeper(
        SchedulerStructs.SubscriptionStatus storage status,
        uint256 startGas,
        uint256 numPriceIds
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

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-29)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```
