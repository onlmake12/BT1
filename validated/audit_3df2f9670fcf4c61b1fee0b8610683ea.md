### Title
Unbounded `tx.gasprice` in Keeper Fee Calculation Allows Subscription Balance Drain - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
The `_processFeesAndPayKeeper()` function in `Scheduler.sol` calculates the keeper reimbursement using `tx.gasprice` with no upper bound. Because `updatePriceFeeds` is permissionless, any caller controls `tx.gasprice`. A malicious keeper can submit the transaction with an artificially inflated gas price to extract far more from a subscription's balance than the legitimate cost of the update, draining subscription funds at an accelerated rate.

### Finding Description
In `Scheduler.sol`, `_processFeesAndPayKeeper` computes the keeper payment as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
// ...
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [1](#0-0) 

`GAS_OVERHEAD` is a fixed constant of 30,000 gas units, intended to cover transaction overhead: [2](#0-1) 

The function is called from the permissionless `updatePriceFeeds`: [3](#0-2) 

There is no cap on `tx.gasprice`. The keeper's net profit per update is always at least `GAS_OVERHEAD * tx.gasprice` (30,000 × gas price), because the keeper is reimbursed for `gasUsed + GAS_OVERHEAD` gas units but only pays for `gasUsed` gas units. By setting an inflated `maxPriorityFeePerGas` (tip) — especially on chains where the keeper controls or has a deal with the block producer — the keeper can make `tx.gasprice` arbitrarily large, extracting `30,000 × inflated_gasprice` wei per update from the subscription balance.

### Impact Explanation
Subscription managers deposit ETH expecting to pay for updates at normal market gas prices. A malicious keeper inflating `tx.gasprice` to, e.g., 10,000 gwei extracts `30,000 × 10,000 gwei = 0.3 ETH` per update in pure profit (beyond actual gas costs). This drains subscription balances far faster than intended, causing subscriptions to run out of funds and stop updating. Protocols relying on those price feeds receive stale prices, which can lead to incorrect liquidations, mispriced derivatives, or other downstream financial harm. [4](#0-3) 

### Likelihood Explanation
`updatePriceFeeds` is explicitly permissionless — anyone can call it with no registration: [5](#0-4) 

The attacker only needs to be a keeper (no privileged role) and control their own transaction's gas price, which is always true. On EIP-1559 chains the attacker sets `maxPriorityFeePerGas` to inflate `tx.gasprice`; on legacy chains `gasPrice` is fully attacker-controlled. The attack is profitable whenever `GAS_OVERHEAD × inflated_gasprice` exceeds the cost of the tip paid to the block producer, which is trivially achievable when the attacker is also the block producer or has a private mempool arrangement.

### Recommendation
Introduce a maximum gas price cap in `_processFeesAndPayKeeper`. For example:

```solidity
uint256 cappedGasPrice = tx.gasprice < MAX_GAS_PRICE ? tx.gasprice : MAX_GAS_PRICE;
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * cappedGasPrice;
```

`MAX_GAS_PRICE` should be a governance-configurable parameter (similar to `singleUpdateKeeperFeeInWei`) set to a reasonable upper bound for the target chain. Alternatively, compute the reimbursement using a time-weighted average gas price oracle rather than the live `tx.gasprice`.

### Proof of Concept
1. Subscription manager creates a subscription with 1 ETH balance and a 60-second heartbeat.
2. Attacker (keeper) waits 60 seconds for the heartbeat condition to be met.
3. Attacker submits `updatePriceFeeds(subscriptionId, updateData)` with `maxPriorityFeePerGas = 10,000 gwei` via a private relay (e.g., Flashbots) so the block producer receives the tip.
4. `tx.gasprice ≈ baseFee + 10,000 gwei`. Suppose `gasUsed = 200,000` and `baseFee = 10 gwei`:
   - Attacker pays: `200,000 × 10,010 gwei = ~2.002 ETH` in gas (tip goes to block producer / self).
   - Attacker receives from subscription: `(200,000 + 30,000) × 10,010 gwei = ~2.302 ETH`.
   - Net extraction from subscription beyond legitimate cost: `30,000 × 10,010 gwei ≈ 0.3 ETH`.
5. The subscription balance is drained by 0.3 ETH more than the legitimate update cost in a single transaction.
6. Repeated over multiple update intervals, the subscription is exhausted prematurely, causing price feed staleness for all downstream consumers. [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L844-863)
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

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L55-56)
```markdown
6.  **Keeper Payment:** The Keeper (`msg.sender`) that successfully lands the update transaction is reimbursed for the transaction costs, plus a premium. The contract dynamically calculates the cost (gas used during the push \* current gas price + fixed overhead + premium) and transfers this amount to the Keeper from the subscription's balance. Payment only occurs if the update conditions were met and the transaction succeeded.
7.  **Reading:** Readers get prices using the `@pythnetwork/pyth-sdk-solidity` SDK. Readers are recommended to use the SDK's functions `get(Ema)PricesNoOlderThan`, which wrap the contract's `get(Ema)PricesUnsafe` functions and validate that the price is recent.
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-62)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
