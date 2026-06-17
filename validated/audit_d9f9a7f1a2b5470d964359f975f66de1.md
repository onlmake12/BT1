### Title
Permanent Subscriptions Lack Any Recovery Mechanism for Monotonically-Increasing `priceLastUpdatedAt` — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler.sol` enforces a strictly-increasing `priceLastUpdatedAt` for every subscription. For **permanent** subscriptions (`isPermanent = true`), `updateSubscription` unconditionally reverts, so there is no path — not even for the subscription manager or the protocol admin — to reset or correct `priceLastUpdatedAt`. Because `updatePriceFeeds` is permissionless and the contract accepts timestamps up to `FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD` (10 seconds) in the future, any caller can advance `priceLastUpdatedAt` to a future value, after which all subsequent updates are rejected until real time catches up. The design flaw is structural: the missing recovery interface for permanent subscriptions is the direct analog of ZetaChain's missing `RemoveBlockHeader` external interface.

---

### Finding Description

`updatePriceFeeds` is callable by anyone with no access control: [1](#0-0) 

It passes `curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD` as the upper bound to the Pyth oracle parser, so price updates with timestamps up to 10 seconds in the future are accepted: [2](#0-1) 

`_validateShouldUpdatePrices` then takes the **maximum** `publishTime` across all feeds in the batch and stores it as `priceLastUpdatedAt`: [3](#0-2) 

Any subsequent call whose maximum `publishTime` is `<= priceLastUpdatedAt` is rejected with `TimestampOlderThanLastUpdate`. This is the monotonic gate.

For non-permanent subscriptions the manager can reset `priceLastUpdatedAt` to zero by calling `updateSubscription` and adding a new price ID: [4](#0-3) 

For **permanent** subscriptions, `updateSubscription` unconditionally reverts at the very first check, before any state is read: [5](#0-4) 

`withdrawFunds` is also blocked for permanent subscriptions: [6](#0-5) 

There is no admin-level emergency function anywhere in the contract to reset `priceLastUpdatedAt` for a permanent subscription. The constant values confirm the window: [7](#0-6) 

---

### Impact Explanation

An unprivileged caller submits a valid Wormhole-attested Pyth price update whose maximum `publishTime` equals `block.timestamp + 10 s`. The Scheduler accepts it, stores `priceLastUpdatedAt = block.timestamp + 10 s`, and all subsequent `updatePriceFeeds` calls for that permanent subscription revert with `TimestampOlderThanLastUpdate` for the next 10 seconds. Because `updateSubscription` is permanently blocked for permanent subscriptions, there is **no on-chain recovery path** — not for the manager, not for the admin. The subscription is operationally halted for the duration. DeFi protocols that rely on permanent subscriptions for time-sensitive operations (liquidations, collateral checks) are exposed during this window.

---

### Likelihood Explanation

`updatePriceFeeds` has no caller restriction. Any keeper, MEV bot, or adversary can submit a valid Pyth VAA whose `publishTime` is within the 10-second future window — a normal occurrence given Pythnet's clock can run slightly ahead of EVM block timestamps. The attacker pays only the Pyth update fee (deducted from the subscription's own balance, not the attacker's). The attack is cheap, repeatable, and requires no privileged access.

---

### Recommendation

1. **Add an admin-level emergency reset function** that can set `priceLastUpdatedAt` to zero (or to a caller-supplied value) for any subscription, including permanent ones. This mirrors the `RemoveBlockHeader` keeper function that ZetaChain already had internally but never exposed.
2. Alternatively, **separate the immutability of subscription parameters from the mutability of operational state**: allow the manager (or admin) to reset `priceLastUpdatedAt` without changing any subscription parameters, even for permanent subscriptions.
3. Consider whether the `FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD` window is necessary at all; tightening it to zero would eliminate the attack surface entirely.

---

### Proof of Concept

```
1. Alice creates a permanent subscription for price feed P with heartbeat = 60 s.
2. At block.timestamp = T, Bob (unprivileged) calls updatePriceFeeds with a valid
   Pyth VAA whose publishTime = T + 10.
   - parsePriceFeedUpdatesWithConfig accepts it (maxPublishTime = T + 10).
   - _validateShouldUpdatePrices: updateTimestamp = T+10 > priceLastUpdatedAt (0) → passes.
   - status.priceLastUpdatedAt is set to T + 10.
3. At block.timestamp = T + 5, a legitimate keeper calls updatePriceFeeds with
   publishTime = T + 5.
   - updateTimestamp = T+5 <= priceLastUpdatedAt (T+10) → revert TimestampOlderThanLastUpdate.
4. Alice tries to call updateSubscription to reset priceLastUpdatedAt.
   - currentParams.isPermanent == true → revert CannotUpdatePermanentSubscription.
5. No admin function exists to reset priceLastUpdatedAt. The subscription is stuck
   until block.timestamp > T + 10 and a new price update with publishTime > T + 10
   is available.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L144-147)
```text
        // Reset priceLastUpdatedAt to 0 if new price IDs were added
        if (newPriceIdsAdded) {
            _state.subscriptionStatuses[subscriptionId].priceLastUpdatedAt = 0;
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-278)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L307-319)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L366-397)
```text
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
        }

        // Calculate the minimum acceptable timestamp (clamped at 0)
        // The maximum acceptable timestamp is enforced by the parsePriceFeedUpdatesWithSlots call
        uint256 minAllowedTimestamp = (block.timestamp >
            PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            : 0;

        // Validate that the update timestamp is not too old
        if (updateTimestamp < minAllowedTimestamp) {
            revert SchedulerErrors.TimestampTooOld(
                updateTimestamp,
                block.timestamp
            );
        }

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-26)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;

    /// Maximum time in the future (relative to current block timestamp)
    /// for which a price update timestamp is considered valid
    uint64 public constant FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD = 10 seconds;
```
