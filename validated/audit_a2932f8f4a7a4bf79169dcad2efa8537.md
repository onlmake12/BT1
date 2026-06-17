### Title
Keeper Can Drain Subscription Balance via Inflated `tx.gasprice` in `_processFeesAndPayKeeper` — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Pulse `Scheduler` contract reimburses keepers for gas costs using `tx.gasprice` with no upper-bound cap. Because the keeper role is permissionless and the keeper is the one who submits the transaction, a malicious keeper can set an arbitrarily high gas price to drain a subscription's entire balance in a single `updatePriceFeeds` call. The subscription manager has no mechanism to specify a maximum gas price they are willing to pay per update.

---

### Finding Description

In `Scheduler.sol`, the `updatePriceFeeds` function records gas at entry and then calls `_processFeesAndPayKeeper` to reimburse the keeper:

```solidity
// Scheduler.sol line 279
uint256 startGas = gasleft();
```

Inside `_processFeesAndPayKeeper`:

```solidity
// Scheduler.sol lines 846–860
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}

status.balanceInWei -= totalKeeperFee;
status.totalSpent += totalKeeperFee;

(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
```

`tx.gasprice` is used directly and is entirely controlled by the keeper submitting the transaction. There is no cap, no maximum gas price parameter accepted from the subscription manager, and no on-chain check that `tx.gasprice` is within a reasonable range. The keeper can set `maxFeePerGas` and `maxPriorityFeePerGas` to extreme values (e.g., thousands of gwei), making `gasCost` orders of magnitude larger than the actual network cost, and extract the full `status.balanceInWei` in a single call (bounded only by the subscription balance check). [1](#0-0) 

---

### Impact Explanation

A subscription manager deposits native tokens into the `Scheduler` contract to fund ongoing price feed updates. A malicious keeper can call `updatePriceFeeds` with a legitimate price update (satisfying all trigger conditions) but with an inflated `tx.gasprice`. The contract will compute a `totalKeeperFee` that equals nearly the entire subscription balance and transfer it to the keeper. The subscription manager loses their deposited funds far in excess of the actual gas cost of the update. This is a direct, irreversible loss of user funds.

---

### Likelihood Explanation

The keeper role is explicitly permissionless — the README states "Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`." Any unprivileged actor can become a keeper and submit a valid update with an inflated gas price. The only constraint is that the update must satisfy the subscription's trigger conditions (heartbeat or deviation threshold), which are observable on-chain and can be timed by the attacker. Likelihood is **Medium**: it requires waiting for a valid trigger window, but no special access is needed. [2](#0-1) 

---

### Recommendation

Introduce a `maxGasPriceInWei` parameter in `SubscriptionParams` that the subscription manager sets at subscription creation or update time. In `_processFeesAndPayKeeper`, cap the effective gas price used for reimbursement:

```solidity
uint256 effectiveGasPrice = Math.min(tx.gasprice, params.maxGasPriceInWei);
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
```

This gives subscription managers slippage-equivalent protection: they specify the maximum gas price they are willing to reimburse, and any keeper submitting above that price is only reimbursed up to the cap. Alternatively, the admin can set a global `maxGasPriceInWei` parameter on the contract. [3](#0-2) 

---

### Proof of Concept

1. Subscription manager creates a subscription with `heartbeatSeconds = 60` and deposits 1 ETH.
2. Attacker waits 60 seconds for the heartbeat trigger to become valid.
3. Attacker calls `updatePriceFeeds(subscriptionId, validUpdateData)` with `maxFeePerGas = 100,000 gwei` and `maxPriorityFeePerGas = 100,000 gwei`.
4. On a chain where `baseFee = 1 gwei`, `tx.gasprice = baseFee + maxPriorityFeePerGas = 100,001 gwei`.
5. Suppose `updatePriceFeeds` uses ~300,000 gas. Then `gasCost = (300,000 + GAS_OVERHEAD) * 100,001 gwei ≈ 30+ ETH`.
6. The contract checks `status.balanceInWei < totalKeeperFee`. Since the subscription only has 1 ETH, the check passes (1 ETH < 30 ETH would revert — but the attacker calibrates `maxFeePerGas` to extract exactly the subscription balance minus 1 wei).
7. Attacker sets `maxFeePerGas` such that `totalKeeperFee ≈ status.balanceInWei`, extracting the full subscription balance while paying only the actual network gas cost (e.g., ~0.0003 ETH at 1 gwei base fee). [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-62)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
