### Title
Unprivileged Caller Can Drain Subscription Balance via Inflated `tx.gasprice` in `updatePriceFeeds` — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updatePriceFeeds` has no access control and pays the caller a keeper fee calculated directly from `tx.gasprice` with no cap. Any unprivileged attacker can call this function whenever update conditions are met and set an arbitrarily high gas price, extracting the subscription's entire balance in a single transaction.

---

### Finding Description

`Scheduler.updatePriceFeeds` is declared `external` with no access-control modifier:

```solidity
function updatePriceFeeds(
    uint256 subscriptionId,
    bytes[] calldata updateData
) external override {
``` [1](#0-0) 

After verifying update conditions, the function calls `_processFeesAndPayKeeper`, which computes the keeper fee as:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;
``` [2](#0-1) 

The fee is then transferred directly to `msg.sender`:

```solidity
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
``` [3](#0-2) 

There is **no cap on `tx.gasprice`** and **no restriction on who can be `msg.sender`**. The only bound is the subscription's balance — the function reverts only if `status.balanceInWei < totalKeeperFee`. [4](#0-3) 

The comment in `_processFeesAndPayKeeper` itself acknowledges the implicit trust assumption that is never enforced:

> `@dev This function sends funds to msg.sender, so be sure that this is being called by a keeper.` [5](#0-4) 

---

### Impact Explanation

An attacker who calls `updatePriceFeeds` with `tx.gasprice = P` extracts:

```
totalKeeperFee = (gasUsed + GAS_OVERHEAD) * P + singleUpdateKeeperFeeInWei * numPriceIds
```

By choosing `P` large enough, the attacker can set `totalKeeperFee` equal to the subscription's entire `balanceInWei`, draining it in a single transaction. The subscription owner funded the contract expecting normal market gas prices; the attacker extracts far more than the legitimate cost of the update. The subscription is left with zero balance and becomes non-functional, disrupting the price-feed service for any downstream consumer. [6](#0-5) 

---

### Likelihood Explanation

- **No privilege required**: any EOA or contract can call `updatePriceFeeds`.
- **Trigger condition is public and predictable**: heartbeat-based subscriptions become eligible every `heartbeatSeconds`; deviation-based subscriptions become eligible whenever the Pyth price moves enough — both conditions are observable on-chain.
- **Gas price is fully attacker-controlled**: the attacker simply submits the transaction with an inflated `gasPrice` field.
- **No front-running needed**: the attacker only needs to be the first caller after conditions are met, which is trivially achievable by monitoring the mempool or the Pyth price feed. [7](#0-6) 

---

### Recommendation

1. **Cap `tx.gasprice`**: introduce a `maxGasPriceInWei` parameter in `SubscriptionParams` (set by the subscription manager) and enforce `tx.gasprice <= maxGasPriceInWei` inside `_processFeesAndPayKeeper`. This mirrors the `maxBid` concept from the AuctionCrowdfund report.

2. **Restrict callers (optional, stronger mitigation)**: add an optional `keeperWhitelist` to `SubscriptionParams`, analogous to the `readerWhitelist` already present. When non-empty, only whitelisted addresses may call `updatePriceFeeds` for that subscription.

3. **Use a reference gas price oracle**: instead of raw `tx.gasprice`, use a time-weighted or block-base-fee reference (e.g., `block.basefee + maxPriorityFeePerGas_cap`) to bound the keeper fee to a reasonable market rate. [8](#0-7) 

---

### Proof of Concept

1. Subscription owner creates a subscription with `heartbeatSeconds = 60` and funds it with `10 ETH`.
2. Attacker monitors the chain; after 60 seconds the heartbeat condition is met.
3. Attacker calls `scheduler.updatePriceFeeds(subscriptionId, updateData)` with `tx.gasprice = 10 ETH / (gasUsed + GAS_OVERHEAD)`.
4. Inside `_processFeesAndPayKeeper`:
   - `gasCost = (gasUsed + 30000) * tx.gasprice ≈ 10 ETH`
   - `totalKeeperFee ≈ 10 ETH`
   - `status.balanceInWei >= totalKeeperFee` passes (barely).
5. The entire `10 ETH` subscription balance is transferred to the attacker's address.
6. The subscription balance is now `0`; all future `updatePriceFeeds` calls revert with `InsufficientBalance`, permanently disabling the subscription. [9](#0-8) [6](#0-5)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L399-410)
```text
        // If updateOnHeartbeat is enabled and the heartbeat interval has passed, trigger update
        if (params.updateCriteria.updateOnHeartbeat) {
            uint256 lastUpdateTime = status.priceLastUpdatedAt;

            if (
                lastUpdateTime == 0 ||
                updateTimestamp >=
                lastUpdateTime + params.updateCriteria.heartbeatSeconds
            ) {
                return updateTimestamp;
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L835-836)
```text
    /// @dev This function sends funds to `msg.sender`, so be sure that this is being called by a keeper.
    /// @dev Note that the Pyth fee is already paid in the parsePriceFeedUpdatesWithSlots call.
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
