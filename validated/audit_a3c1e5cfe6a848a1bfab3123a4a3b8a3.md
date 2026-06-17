### Title
Permissionless Keeper Can Drain Subscription Balances via Inflated `tx.gasprice` in `_processFeesAndPayKeeper` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Pulse `Scheduler` contract pays permissionless keepers for price updates using a gas-reimbursement formula that includes a fixed `GAS_OVERHEAD` multiplied by `tx.gasprice`. Because `tx.gasprice` is fully attacker-controlled and there is no cap, a malicious keeper can set an arbitrarily high gas price to extract a disproportionate payment from any subscription balance on every valid update, draining subscription funds far faster than the subscription manager intended.

---

### Finding Description

`Scheduler.updatePriceFeeds` is callable by any address with no registration or whitelist requirement. At the end of a successful update, `_processFeesAndPayKeeper` computes the keeper's payment as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
```

`GAS_OVERHEAD` is a fixed constant of `30000` gas units. The keeper controls `tx.gasprice` entirely. The keeper's net profit per update is:

```
profit = tx.gasprice * GAS_OVERHEAD + keeperSpecificFee
```

because the subscription reimburses `(actualGasUsed + GAS_OVERHEAD) * tx.gasprice` while the keeper only spends `actualGasUsed * tx.gasprice` on-chain. There is no upper bound on `tx.gasprice` enforced anywhere in the contract. A keeper who sets `tx.gasprice = 10,000 gwei` extracts `30,000 * 10,000 gwei = 0.3 ETH` of pure profit from the subscription per update, versus the ~`0.003 ETH` expected at 1 gwei. The only guard is `InsufficientBalance`, which simply stops the attack once the subscription is empty.

The keeper network is explicitly permissionless by design:

> "Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`."

A malicious keeper can therefore target any well-funded subscription, call `updatePriceFeeds` at every valid trigger interval with an inflated gas price, and drain the subscription balance orders of magnitude faster than the subscription manager anticipated.

---

### Impact Explanation

Subscription managers deposit ETH into the contract to fund price updates for their protocols. A malicious keeper can drain these balances by submitting valid updates with an inflated `tx.gasprice`. Once the balance falls below `minimumBalance`, the subscription is deactivated and prices stop updating. The attack is indistinguishable from a legitimate keeper operating on a high-gas-price chain. Subscription managers' deposited funds are at direct risk of accelerated extraction.

---

### Likelihood Explanation

The keeper network is permissionless — no registration, no whitelist, no stake. Any EOA can call `updatePriceFeeds`. The attack requires only that the trigger condition (heartbeat or deviation) is met, which happens on every normal update cycle. The attacker profits on every single valid update call. The attack is economically rational whenever `GAS_OVERHEAD * tx.gasprice > actual_gas_cost_of_inflated_tip`, which is always true since the overhead profit is additive.

---

### Recommendation

Cap the effective `tx.gasprice` used in the keeper fee calculation to a protocol-configured maximum (e.g., `min(tx.gasprice, maxGasPriceInWei)`), where `maxGasPriceInWei` is an admin-settable parameter. This mirrors the mitigation used in similar keeper-payment systems. Alternatively, use a time-weighted average gas price oracle rather than the raw `tx.gasprice` of the keeper's transaction.

---

### Proof of Concept

1. Subscription manager creates a subscription with 1 ETH balance, heartbeat = 60 seconds.
2. Malicious keeper waits 60 seconds for the heartbeat to expire.
3. Malicious keeper calls `updatePriceFeeds` with `tx.gasprice = 100,000 gwei` (a valid but inflated tip on EIP-1559 chains via `maxPriorityFeePerGas`).
4. `_processFeesAndPayKeeper` computes: `gasCost = (actualGas + 30000) * 100000 gwei`. With `actualGas ≈ 200,000`, this is `230,000 * 100,000 gwei = 23 ETH` — exceeding the subscription balance, so the call reverts with `InsufficientBalance`.
5. Keeper lowers to `tx.gasprice = 1,000 gwei`: `gasCost = 230,000 * 1,000 gwei = 0.23 ETH` deducted per update. Keeper paid `200,000 * 1,000 gwei = 0.2 ETH` in gas. Net profit = `30,000 * 1,000 gwei = 0.03 ETH` per update.
6. At a 60-second heartbeat, the 1 ETH subscription is drained in ~33 updates (~33 minutes) instead of the hundreds of updates expected at normal gas prices.

**Relevant code:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L27-30)
```text
    /// Fixed gas overhead component used in keeper fee calculation.
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
}
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L58-62)
```markdown
### Keeper Network & Incentives

- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
