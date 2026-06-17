### Title
Keeper Fee Theft and DoS via Frontrunning `updatePriceFeeds` with Identical Update Data — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updatePriceFeeds` has no caller access control and enforces a strict `updateTimestamp <= status.priceLastUpdatedAt` revert check. An attacker can copy a legitimate keeper's pending `updatePriceFeeds` calldata from the mempool, submit it with a higher gas price, and have their transaction land first. The attacker's transaction succeeds (collecting the keeper fee paid from the subscription balance), while the legitimate keeper's transaction reverts with `TimestampOlderThanLastUpdate`. Repeated execution of this pattern constitutes sustained keeper-fee theft and a DoS on legitimate keepers.

---

### Finding Description

`updatePriceFeeds` is a permissionless `external` function — any address may call it for any active subscription. [1](#0-0) 

After deducting the Pyth oracle fee and parsing the price feeds, the function calls `_validateShouldUpdatePrices`, which enforces:

```solidity
if (
    status.priceLastUpdatedAt > 0 &&
    updateTimestamp <= status.priceLastUpdatedAt
) {
    revert SchedulerErrors.TimestampOlderThanLastUpdate(
        updateTimestamp,
        status.priceLastUpdatedAt
    );
}
``` [2](#0-1) 

This is a strict inequality check on the update timestamp. Because Pyth price-update VAAs are public (fetched from the Pyth price service), any observer can obtain the exact same `updateData` bytes that a keeper is about to submit.

Attack flow:

1. Keeper K fetches a fresh Pyth price update with publish timestamp `T` and submits `updatePriceFeeds(subscriptionId, updateData)`.
2. Attacker A sees the pending transaction in the mempool, copies the identical `updateData`, and resubmits with a higher gas price.
3. A's transaction is mined first: `status.priceLastUpdatedAt` is set to `T`; A receives the keeper fee via `_processFeesAndPayKeeper`.
4. K's transaction is mined next: `updateTimestamp (T) <= status.priceLastUpdatedAt (T)` → reverts with `TimestampOlderThanLastUpdate`. K loses gas and receives no keeper fee. [3](#0-2) 

The attacker can automate this with a MEV bot, frontrunning every keeper update for every subscription.

---

### Impact Explanation

- **Keeper fee theft**: The attacker collects keeper fees that were intended for the legitimate keeper. The keeper fee is paid from the subscription's `balanceInWei` to `msg.sender`.
- **Keeper DoS**: Legitimate keepers consistently fail to land their transactions and receive no compensation for gas spent. Rational keepers will stop submitting updates.
- **Subscription liveness risk**: If all legitimate keepers are driven out, the attacker controls whether subscriptions are updated. The attacker can selectively stop updating subscriptions, causing stale prices for downstream consumers. [4](#0-3) 

---

### Likelihood Explanation

- Pyth price update VAAs are publicly available from the Hermes price service; any attacker can obtain the same `updateData` bytes.
- `updatePriceFeeds` has no access control, making it trivially callable by any EOA or contract.
- MEV infrastructure on EVM chains (Flashbots, etc.) makes mempool frontrunning straightforward and economically rational when keeper fees are involved.
- The attack is profitable: the attacker earns keeper fees while only paying gas, and the keeper fee is drawn from the subscription balance (not the attacker's own funds).

---

### Recommendation

Restrict `updatePriceFeeds` to a whitelisted set of keeper addresses, or implement a commit-reveal / off-chain coordination scheme so that only the first submitter of a given update in a given block is eligible for the keeper fee. Alternatively, record the keeper address at request time (e.g., via a signed intent) so that frontrunners cannot claim the fee even if they land the transaction first.

---

### Proof of Concept

```solidity
// SPDX-License-Identifier: Apache 2
pragma solidity ^0.8.0;

interface IScheduler {
    function updatePriceFeeds(uint256 subscriptionId, bytes[] calldata updateData) external;
}

contract KeeperFrontrunAttack {
    IScheduler scheduler;

    constructor(address _scheduler) {
        scheduler = IScheduler(_scheduler);
    }

    // Called by attacker's MEV bot with the same updateData copied from the
    // legitimate keeper's pending mempool transaction, submitted with higher gas.
    function frontrunKeeper(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external {
        // Attacker lands first: sets priceLastUpdatedAt = T, receives keeper fee
        scheduler.updatePriceFeeds(subscriptionId, updateData);
        // Legitimate keeper's identical call now reverts:
        //   TimestampOlderThanLastUpdate(T, T)
    }
}
```

The legitimate keeper's transaction reverts at `_validateShouldUpdatePrices` because `updateTimestamp (T) <= status.priceLastUpdatedAt (T)`. [2](#0-1)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-288)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-347)
```text
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L388-397)
```text
        // Reject updates if they're older than the latest stored ones
        if (
            status.priceLastUpdatedAt > 0 &&
            updateTimestamp <= status.priceLastUpdatedAt
        ) {
            revert SchedulerErrors.TimestampOlderThanLastUpdate(
                updateTimestamp,
                status.priceLastUpdatedAt
            );
        }
```
