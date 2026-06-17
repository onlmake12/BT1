### Title
Uncapped `tx.gasprice` in Keeper Fee Calculation Allows Subscription Balance Drain via Gas Price Inflation — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `_processFeesAndPayKeeper` function in `Scheduler.sol` computes keeper reimbursement using `tx.gasprice` with no upper bound. Because the fixed `GAS_OVERHEAD` constant is multiplied by the caller-controlled gas price, any unprivileged keeper can submit `updatePriceFeeds` with an artificially inflated gas price, extracting a profit proportional to `GAS_OVERHEAD × tx.gasprice` from the subscription balance while draining that balance far faster than the subscription manager anticipated.

---

### Finding Description

`_processFeesAndPayKeeper` calculates the total keeper fee as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [1](#0-0) 

`GAS_OVERHEAD` is a fixed constant of `30 000` gas units: [2](#0-1) 

The keeper's net profit per call is therefore:

```
net_profit = GAS_OVERHEAD × tx.gasprice + singleUpdateKeeperFeeInWei × numPriceIds
```

Because `tx.gasprice` is fully under the caller's control (the keeper sets `gasPrice` on legacy chains, or `maxPriorityFeePerGas` on EIP-1559 chains), a keeper can inflate it arbitrarily. The keeper pays `gas_used × tx.gasprice` to the network and receives `(gas_used + GAS_OVERHEAD) × tx.gasprice` from the subscription. The `GAS_OVERHEAD` component is pure profit that scales linearly with the chosen gas price.

There is no cap, oracle check, or sanity bound on `tx.gasprice` anywhere in `updatePriceFeeds` or `_processFeesAndPayKeeper`. [3](#0-2) 

The `updatePriceFeeds` entry point is fully permissionless — no registration or whitelist is required to be a keeper: [4](#0-3) 

---

### Impact Explanation

A malicious keeper inflates `tx.gasprice` to `P_inflated` (e.g., 1 000× the market rate `P_market`). Per update:

| | Market-rate keeper | Malicious keeper |
|---|---|---|
| Gas paid to network | `gas_used × P_market` | `gas_used × P_inflated` |
| Received from subscription | `(gas_used + 30 000) × P_market` | `(gas_used + 30 000) × P_inflated` |
| Net profit | `30 000 × P_market` | `30 000 × P_inflated` |
| Subscription drained per update | `(gas_used + 30 000) × P_market` | `(gas_used + 30 000) × P_inflated` |

With `gas_used ≈ 200 000`, `P_market = 10 gwei`, `P_inflated = 10 000 gwei`:
- Honest keeper profit: `30 000 × 10 gwei = 300 000 gwei ≈ 0.0003 ETH`
- Malicious keeper profit: `30 000 × 10 000 gwei = 300 000 000 gwei ≈ 0.3 ETH` per update
- Subscription drained per update: `≈ 2.3 ETH` instead of `≈ 0.0023 ETH`

The subscription balance is exhausted orders of magnitude faster than the manager expected, causing premature deactivation of the subscription and loss of deposited funds to the malicious keeper. Subscription managers have no on-chain recourse once the balance is drained.

---

### Likelihood Explanation

- `updatePriceFeeds` is permissionless; no staking, registration, or whitelist is required.
- The attack is profitable whenever `GAS_OVERHEAD × (P_inflated − P_market) > 0`, which is always true.
- On EIP-1559 chains the keeper sets `maxPriorityFeePerGas` high; on legacy chains they set `gasPrice` directly. Both are trivially achievable.
- The trigger condition (heartbeat or deviation threshold) must be met, but a keeper monitoring the mempool can wait for the natural trigger window and then submit with an inflated gas price, frontrunning honest keepers.

---

### Recommendation

1. **Cap `tx.gasprice` in the fee calculation** to a governance-controlled `maxGasPriceInWei` parameter:
   ```solidity
   uint256 effectiveGasPrice = tx.gasprice < _state.maxGasPriceInWei
       ? tx.gasprice
       : _state.maxGasPriceInWei;
   uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * effectiveGasPrice;
   ```
2. Alternatively, use a trusted on-chain gas price oracle (e.g., EIP-1559 `block.basefee`) as the reference price instead of `tx.gasprice`.
3. Consider requiring keepers to submit a `maxGasPrice` parameter at call time and revert if `tx.gasprice` exceeds it, so subscription managers can bound their exposure.

---

### Proof of Concept

1. Subscription manager calls `createSubscription` with 2 ETH balance, 3 price feeds, heartbeat = 60 s.
2. After 60 s, the heartbeat trigger fires. Malicious keeper submits `updatePriceFeeds(subscriptionId, updateData)` with `gasPrice = 10 000 gwei` (market rate: 10 gwei).
3. Inside `_processFeesAndPayKeeper`:
   - `gasCost = (200 000 + 30 000) × 10 000 gwei = 2 300 000 000 gwei = 2.3 ETH`
   - `keeperSpecificFee = singleUpdateKeeperFeeInWei × 3`
   - `totalKeeperFee ≈ 2.3 ETH`
4. The subscription balance (2 ETH) is insufficient → `InsufficientBalance` revert, OR if balance is larger, the entire balance is drained in a single update.
5. Keeper's actual gas cost: `200 000 × 10 000 gwei = 2 ETH`. Keeper receives `2.3 ETH`. Net profit: `0.3 ETH`.
6. Subscription is deactivated after one update; manager loses deposited funds. [5](#0-4) [3](#0-2)

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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L28-29)
```text
    /// This is a rough estimate of the tx overhead for a keeper to call updatePriceFeeds.
    uint256 public constant GAS_OVERHEAD = 30000;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-60)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.
```
