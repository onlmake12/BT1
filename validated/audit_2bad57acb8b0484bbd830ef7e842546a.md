### Title
Permanent Subscription Price Feeds Cannot Be Removed, Locking Funds If a Feed Is Deprecated — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The `isPermanent` flag in the Pulse `Scheduler` contract prevents any modification to a subscription, including removing price feeds, deactivating the subscription, or withdrawing its balance. If Pyth deprecates a price feed that is part of a permanent subscription, `updatePriceFeeds` will always revert because the keeper can no longer supply valid update data for the deprecated feed. The subscription's ETH balance becomes permanently locked with no admin or owner rescue path.

---

### Finding Description

The `Scheduler.sol` contract supports a `isPermanent` flag on subscriptions. When set, `updateSubscription` unconditionally reverts:

```solidity
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [1](#0-0) 

This means no price feed can ever be removed from a permanent subscription. Similarly, `withdrawFunds` reverts for permanent subscriptions:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
``` [2](#0-1) 

The `updatePriceFeeds` function calls `pyth.parsePriceFeedUpdatesWithConfig` passing the full `params.priceIds` array, requiring valid update data for **every** price feed in the subscription:

```solidity
) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
        updateData,
        params.priceIds,
        0,
        curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
        false,
        true,
        false
    );
``` [3](#0-2) 

If Pyth deprecates any one of the price feeds in `params.priceIds`, keepers can no longer supply valid update data for it. The `parsePriceFeedUpdatesWithConfig` call reverts, making `updatePriceFeeds` permanently uncallable for that subscription. Because the subscription is permanent, neither the manager nor any admin can:

- Remove the deprecated feed from `priceIds`
- Deactivate the subscription
- Withdraw the locked ETH balance

The balance drains to zero only if updates succeed; if they never succeed again, the ETH is permanently trapped.

---

### Impact Explanation

**Impact: High.** Any ETH deposited into a permanent subscription becomes permanently inaccessible if a single price feed in the subscription is deprecated by Pyth. There is no admin rescue function, no emergency deactivation path, and no way to remove the offending feed. The subscription's entire balance is locked forever.

---

### Likelihood Explanation

**Likelihood: Low.** Pyth does deprecate price feeds over time (e.g., when assets are delisted or feeds are consolidated). A permanent subscription created today with a feed that is later deprecated will trigger this condition. The condition is not attacker-controlled but is a realistic operational event.

---

### Recommendation

1. Add an admin/governance-controlled function to forcibly remove a price feed from a permanent subscription, or to rescue funds from a permanent subscription whose `updatePriceFeeds` is permanently broken.
2. Alternatively, allow the contract admin to override the `isPermanent` lock in emergency scenarios (e.g., a deprecated feed), similar to how the backstop pool owner should be able to remove covered swap pools.
3. Consider whether `isPermanent` should truly prevent deactivation and fund withdrawal, or only prevent parameter changes.

---

### Proof of Concept

1. Alice calls `createSubscription` with `isPermanent = true`, including price feed `0xABC` in `priceIds`, and deposits 10 ETH.
2. Pyth deprecates feed `0xABC`; no new VAAs are published for it.
3. A keeper attempts `updatePriceFeeds(subscriptionId, updateData)`. The call to `parsePriceFeedUpdatesWithConfig` reverts because `updateData` cannot contain a valid entry for the deprecated `0xABC` feed.
4. Alice calls `withdrawFunds` → reverts: `CannotUpdatePermanentSubscription`.
5. Alice calls `updateSubscription` to remove `0xABC` → reverts: `CannotUpdatePermanentSubscription`.
6. The 10 ETH is permanently locked in the contract with no recovery path. [1](#0-0) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-319)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L630-642)
```text
    function withdrawFunds(
        uint256 subscriptionId,
        uint256 amount
    ) external override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```
