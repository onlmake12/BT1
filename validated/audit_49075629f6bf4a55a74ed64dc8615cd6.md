### Title
Keeper Can DoS `updatePriceFeeds` by Reverting on ETH Receipt, Blocking Subscription Price Updates - (File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol)

---

### Summary

In `Scheduler.sol`, the internal `_processFeesAndPayKeeper` function pays the keeper (`msg.sender`) via a low-level ETH call at the **end** of `updatePriceFeeds`. If the keeper's contract reverts on receiving ETH, the entire `updatePriceFeeds` transaction reverts — including all price-storage state changes — preventing subscription price updates from being committed on-chain.

---

### Finding Description

`updatePriceFeeds` is a permissionless function callable by any external address acting as a keeper. After parsing and validating price feeds, storing them, and updating subscription status, it calls `_processFeesAndPayKeeper`:

```solidity
// Scheduler.sol L860-863
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
if (!sent) {
    revert SchedulerErrors.KeeperPaymentFailed();
}
``` [1](#0-0) 

Because this call is the **last step** of `updatePriceFeeds`, a revert here unwinds all preceding state changes in the same transaction:

- `status.balanceInWei -= pythFee` (line 305)
- `status.priceLastUpdatedAt = latestPublishTime` (line 340)
- `_storePriceUpdates(subscriptionId, priceFeeds)` (line 343)
- `status.balanceInWei -= totalKeeperFee` (line 856) [2](#0-1) 

A malicious keeper deploys a contract whose `receive()` or `fallback()` reverts unconditionally. When this keeper calls `updatePriceFeeds` with fully valid price data, the ETH transfer fails, `KeeperPaymentFailed` is thrown, and the entire transaction reverts. No price update is stored for the subscription.

---

### Impact Explanation

Subscription owners relying on Scheduler for on-chain price freshness receive no update for every transaction submitted by the malicious keeper. If the attacker front-runs honest keepers (e.g., by paying higher gas), they can sustain the DoS and keep subscription prices stale indefinitely. Stale prices in downstream DeFi protocols (lending, derivatives, liquidations) that read from Scheduler subscriptions can lead to incorrect liquidations, mispriced collateral, or protocol insolvency. The subscription's ETH balance is also not consumed, so the attack is essentially free beyond the attacker's own gas cost.

---

### Likelihood Explanation

`updatePriceFeeds` has **no access control** — any address may call it. A malicious actor needs only to deploy a contract with a reverting `receive()` function and call `updatePriceFeeds` with valid Pyth update data. The attack requires no privileged role, no leaked key, and no governance majority. Valid price data is publicly available from Pyth's price service.

---

### Recommendation

Move the keeper payment **before** the price-storage state changes (checks-effects-interactions), or — more robustly — handle payment failure without reverting the entire transaction. Emit a `KeeperPaymentFailed` event and allow the price update to be committed even if the ETH transfer fails, similar to how `Entropy.sol`'s new callback flow uses `excessivelySafeCall` to isolate external call failures:

```solidity
(bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
if (!sent) {
    emit KeeperPaymentFailed(msg.sender, totalKeeperFee);
    // Restore balance rather than reverting the whole tx
    status.balanceInWei += totalKeeperFee;
    status.totalSpent -= totalKeeperFee;
}
```

This ensures price updates are always committed regardless of keeper contract behavior.

---

### Proof of Concept

```solidity
// Malicious keeper contract
contract MaliciousKeeper {
    receive() external payable {
        revert("no ETH accepted");
    }

    function attack(address scheduler, uint256 subscriptionId, bytes[] calldata updateData) external {
        // Calls updatePriceFeeds with valid data; payment reverts → entire tx reverts
        IScheduler(scheduler).updatePriceFeeds(subscriptionId, updateData);
    }
}
```

1. Deploy `MaliciousKeeper`.
2. Call `attack(schedulerAddress, targetSubscriptionId, validUpdateData)`.
3. `_processFeesAndPayKeeper` attempts `msg.sender.call{value: fee}("")` → `MaliciousKeeper.receive()` reverts.
4. `KeeperPaymentFailed` is thrown → entire `updatePriceFeeds` reverts.
5. Subscription's `priceLastUpdatedAt` and stored price feeds remain unchanged.
6. Repeat (or front-run honest keepers) to sustain stale prices. [3](#0-2)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L305-346)
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
