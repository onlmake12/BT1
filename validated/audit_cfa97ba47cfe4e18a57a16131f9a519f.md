### Title
Unbounded Gas Price in Keeper Fee Calculation Allows Block Builder to Drain Subscription Balances - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updatePriceFeeds` pays keepers a gas rebate computed as `(gasUsed + GAS_OVERHEAD) * tx.gasprice` with no cap on `tx.gasprice`. A block builder acting as a keeper can set an arbitrarily inflated gas price and extract the full `balanceInWei` of any subscription in a single transaction.

---

### Finding Description

`updatePriceFeeds` is a permissionless function callable by any keeper. At entry it snapshots `startGas = gasleft()`, performs work, then calls `_processFeesAndPayKeeper`:

```solidity
// Scheduler.sol line 279
uint256 startGas = gasleft();
// ... computation ...
_processFeesAndPayKeeper(status, startGas, params.priceIds.length); // line 345
```

Inside `_processFeesAndPayKeeper`:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice; // line 846
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
status.balanceInWei -= totalKeeperFee;
(bool sent, ) = msg.sender.call{value: totalKeeperFee}(""); // line 860
```

There is no upper bound on `tx.gasprice`. The only constraint is `status.balanceInWei`, which acts as a ceiling — meaning the attacker can extract up to the subscription's entire ETH balance in one call. [1](#0-0) [2](#0-1) 

---

### Impact Explanation

A block builder who calls `updatePriceFeeds` with `tx.gasprice` set to `status.balanceInWei / (gasUsed + GAS_OVERHEAD)` will receive the subscription's entire ETH balance as a "gas rebate," far exceeding the real cost of the transaction. Subscription owners lose all deposited funds. Because `updatePriceFeeds` has no access control, any block builder can target any active subscription. [3](#0-2) 

---

### Likelihood Explanation

Block builders do not pay for gas themselves — they include their own transactions for free. This makes inflating `tx.gasprice` costless for them. The function is fully permissionless (no whitelist, no role check), so any block builder can call it for any subscription at any time. The only prerequisite is that a valid price update exists satisfying the subscription's trigger criteria, which is routinely available from Pyth's public data feeds. [4](#0-3) 

---

### Recommendation

Cap `tx.gasprice` to a protocol-defined maximum (e.g., a governance-settable `maxGasPriceInWei`) before computing `gasCost`:

```solidity
uint256 effectiveGasPrice = tx.gasprice > _state.maxGasPriceInWei
    ? _state.maxGasPriceInWei
    : tx.gasprice;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

Alternatively, cap `totalKeeperFee` to a multiple of `keeperSpecificFee` or a fixed maximum per update.

---

### Proof of Concept

1. Attacker registers as a block builder on the target chain.
2. A subscription with `balanceInWei = B` exists and its heartbeat/deviation trigger is satisfiable.
3. Attacker obtains valid `updateData` from Pyth's public API.
4. Attacker includes a self-built transaction calling `updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = B / (expectedGasUsed + GAS_OVERHEAD)`.
5. `_processFeesAndPayKeeper` computes `gasCost ≈ B`, passes the `balanceInWei >= totalKeeperFee` check, deducts `B` from the subscription, and transfers `B` wei to the attacker.
6. Subscription balance is fully drained; the actual ETH cost to the attacker is zero (block builder pays no gas). [5](#0-4)

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
