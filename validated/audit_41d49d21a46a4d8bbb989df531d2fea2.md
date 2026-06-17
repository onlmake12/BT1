### Title
Keeper-Controlled `tx.gasprice` Inflates `GAS_OVERHEAD` Fee Component, Draining Subscription Balances - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler._processFeesAndPayKeeper`, the keeper fee includes `GAS_OVERHEAD * tx.gasprice`, where `GAS_OVERHEAD = 30,000` is a fixed constant and `tx.gasprice` is entirely controlled by the keeper (the unprivileged caller of `updatePriceFeeds`). Because there is no cap on `tx.gasprice`, any keeper can set an arbitrarily high gas price to extract `GAS_OVERHEAD * tx.gasprice` as pure profit per update, draining subscription balances far faster than owners expect.

---

### Finding Description

`Scheduler.updatePriceFeeds` is a permissionless function — any address can call it as a keeper. At the end of each successful update, `_processFeesAndPayKeeper` is called:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`GAS_OVERHEAD = 30_000` is a constant representing estimated transaction overhead: [2](#0-1) 

The keeper pays `actual_gas_used * tx.gasprice` to the network, but receives `(actual_gas_used + GAS_OVERHEAD) * tx.gasprice + keeperSpecificFee` from the subscription. The keeper's **net profit** per update is:

```
net_profit = GAS_OVERHEAD * tx.gasprice + keeperSpecificFee
           = 30,000 * tx.gasprice + keeperSpecificFee
```

By setting `tx.gasprice` to an extreme value (e.g., 10,000 gwei instead of 10 gwei), the keeper extracts `30,000 * 10,000 gwei = 0.3 ETH` in pure profit per update, while the subscription balance is drained at 1,000× the expected rate.

The entry point is fully permissionless: [3](#0-2) 

---

### Impact Explanation

Subscription owners fund their subscriptions expecting to pay for updates at prevailing market gas prices. Because `tx.gasprice` is unbounded and keeper-controlled, a malicious keeper can:

1. **Extract disproportionate profit** — the `GAS_OVERHEAD` component scales linearly with `tx.gasprice`, yielding `30,000 * tx.gasprice` in profit per call regardless of actual gas cost.
2. **Drain subscription balances in a single update** — if the subscription balance is large and `tx.gasprice` is set high enough, the entire balance can be swept in one transaction (subject only to the `InsufficientBalance` check).
3. **Grief subscription owners** — even without profit motive, a keeper can set a very high gas price to rapidly exhaust a subscription's balance, causing it to go inactive and stop serving price updates to dependent protocols. [4](#0-3) 

---

### Likelihood Explanation

- `updatePriceFeeds` has **no access control** — any EOA or contract can call it.
- Setting a high `tx.gasprice` (or `maxPriorityFeePerGas` on EIP-1559 chains) is trivial and costs nothing beyond the gas itself.
- The keeper is net-profitable as long as `GAS_OVERHEAD * tx.gasprice > 0`, which is always true for any nonzero gas price.
- The attack is repeatable on every heartbeat or deviation trigger, compounding the drain.

---

### Recommendation

Cap `tx.gasprice` at a reasonable maximum before using it in the fee calculation. For example:

```solidity
uint256 effectiveGasPrice = tx.gasprice < MAX_GAS_PRICE ? tx.gasprice : MAX_GAS_PRICE;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

Where `MAX_GAS_PRICE` is a governance-configurable parameter (e.g., `500 gwei`). Alternatively, use `block.basefee` (EIP-1559) as a more manipulation-resistant gas price reference, since it is set by the protocol and cannot be inflated by the transaction sender.

---

### Proof of Concept

1. Subscription owner creates a subscription with 1 ETH balance, 2 price feeds, heartbeat = 60s.
2. Attacker (keeper) waits for the heartbeat interval to elapse.
3. Attacker calls `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 100,000 gwei`.
4. Assume actual gas used = 250,000:
   - `gasCost = (250,000 + 30,000) * 100,000 gwei = 28,000,000,000 gwei = 28 ETH`
   - If subscription balance < 28 ETH, the call reverts with `InsufficientBalance`.
   - If subscription balance ≥ 28 ETH, the entire balance is swept to the attacker.
5. Even at moderate inflation (e.g., `tx.gasprice = 1,000 gwei` vs. market 10 gwei):
   - Attacker pays: `250,000 * 1,000 gwei = 0.25 ETH`
   - Attacker receives: `280,000 * 1,000 gwei = 0.28 ETH`
   - Net profit: `30,000 * 1,000 gwei = 0.03 ETH` per update, with subscription drained 100× faster than expected. [5](#0-4) [4](#0-3) [2](#0-1)

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
