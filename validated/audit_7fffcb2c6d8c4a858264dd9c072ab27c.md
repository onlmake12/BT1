### Title
Keeper Fee Inflation via Uncapped `tx.gasprice` in Scheduler `updatePriceFeeds` — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` computes the keeper fee as `(startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice`. Because `tx.gasprice` is a caller-controlled parameter with no upper bound enforced by the contract, any caller can set an arbitrarily high gas price to inflate their keeper fee and drain a subscription's balance in a single transaction.

---

### Finding Description

`Scheduler.updatePriceFeeds` is permissionless — any address may call it. After parsing and validating the price update, it calls `_processFeesAndPayKeeper`, which computes:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`tx.gasprice` is the EIP-1559 effective gas price (`baseFee + priorityFee`). The caller controls `maxPriorityFeePerGas` freely. There is no cap on `totalKeeperFee` beyond the subscription balance check:

```solidity
if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [2](#0-1) 

The `GAS_OVERHEAD` constant is added to the measured gas to cover post-measurement costs. Because it is multiplied by `tx.gasprice`, a caller who sets a high `maxPriorityFeePerGas` extracts `GAS_OVERHEAD * tx.gasprice` in excess of their real gas expenditure. The `updateData` is validated by Wormhole signatures and `checkUpdateDataIsMinimal = true`, so the attacker cannot inflate gas usage through padding — but they do not need to: inflating `tx.gasprice` alone is sufficient. [3](#0-2) 

The `tx.gasprice` parameter is analogous to the unvalidated `airdropAmount` in the reference report: it is caller-controlled, not included in any signed or committed payload, and directly governs how much ETH is transferred out of the subscription balance.

---

### Impact Explanation

A malicious caller can drain an entire subscription balance in one transaction. The attacker's net profit per call is:

```
profit = GAS_OVERHEAD * tx.gasprice + singleUpdateKeeperFeeInWei * numPriceIds
```

For a subscription with balance `B`, the attacker sets:

```
tx.gasprice = B / (gasUsed + GAS_OVERHEAD)
```

At that price the subscription is fully drained, the attacker pays `gasUsed * tx.gasprice` in real gas, and pockets `GAS_OVERHEAD * tx.gasprice` net. For a 1 ETH subscription with `gasUsed ≈ 200 000` and `GAS_OVERHEAD` in the tens of thousands, the required gas price is in the low-thousands-of-gwei range — achievable on any EVM chain. Subscription owners suffer direct, irreversible loss of deposited funds.

---

### Likelihood Explanation

- `updatePriceFeeds` is public and permissionless; no role check exists.
- Setting `maxPriorityFeePerGas` to an arbitrary value requires no special access.
- Valid `updateData` (a Wormhole-signed VAA) is freely available from Hermes.
- The attack is profitable whenever `GAS_OVERHEAD > 0`, which is always true by design.

---

### Recommendation

1. **Cap the reimbursed gas price**: store a `maxKeeperGasPrice` parameter (governable) and use `min(tx.gasprice, maxKeeperGasPrice)` in the fee calculation.
2. **Alternatively**, replace `tx.gasprice` with a time-weighted or block-base-fee oracle so the keeper cannot inflate the price component.
3. **Restrict callers** to a whitelist of registered keepers if the permissionless model is not a hard requirement.

---

### Proof of Concept

```
Preconditions:
  - subscriptionId has balanceInWei = 1 ETH
  - gasUsed ≈ 200 000, GAS_OVERHEAD = 50 000 (illustrative)
  - singleUpdateKeeperFeeInWei = 0

Attack:
  1. Attacker obtains valid updateData from Hermes for the subscription's priceIds.
  2. Attacker submits:
       scheduler.updatePriceFeeds{gasPrice: 4000 gwei}(subscriptionId, updateData)
  3. _processFeesAndPayKeeper computes:
       gasCost = (200 000 + 50 000) * 4 000 gwei = 1 000 000 000 gwei = 1 ETH
       totalKeeperFee = 1 ETH
  4. Subscription balance passes the check (1 ETH >= 1 ETH).
  5. Attacker receives 1 ETH; pays 200 000 * 4 000 gwei = 0.8 ETH in gas.
  6. Net profit: 0.2 ETH. Subscription fully drained.
``` [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L279-346)
```text
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
