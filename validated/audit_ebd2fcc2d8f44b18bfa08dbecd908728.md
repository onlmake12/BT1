### Title
Deviation Check Bypassed on First Update Allows Keeper to Drain Subscription Funds Without Price Movement — (`Scheduler.sol`)

### Summary

In `_validateShouldUpdatePrices`, when a subscription is configured with `updateOnDeviation = true` and `updateOnHeartbeat = false`, the deviation threshold check is bypassed for **all** price feeds whenever **any single** feed has no stored baseline (`previousFeed.id == bytes32(0)`). Any unprivileged keeper can call `updatePriceFeeds` and collect fees without any price movement having occurred.

### Finding Description

`_validateShouldUpdatePrices` in `Scheduler.sol` iterates through price feeds to check deviation. When it encounters a feed with no stored data, it immediately returns `updateTimestamp` for the entire function — bypassing the deviation check for every other feed in the subscription:

```solidity
// If updateOnDeviation is enabled, check if any price has deviated enough
if (params.updateCriteria.updateOnDeviation) {
    for (uint8 i = 0; i < priceFeeds.length; i++) {
        PythStructs.PriceFeed storage previousFeed = _state
            .priceUpdates[subscriptionId][priceFeeds[i].id];

        // If there's no previous price, this is the first update
        if (previousFeed.id == bytes32(0)) {
            return updateTimestamp;   // <-- returns for the WHOLE function
        }
        // ...deviation math for existing feeds never reached...
    }
}
``` [1](#0-0) 

This bypass is triggered in two reachable paths:

**Path 1 — Subscription creation.** `createSubscription` initialises `priceLastUpdatedAt = 0` and stores no price data. All feeds have `previousFeed.id == bytes32(0)`, so the very first `updatePriceFeeds` call is unconditionally accepted. This is intentional for bootstrapping. [2](#0-1) 

**Path 2 — `updateSubscription` adds new price IDs.** When the manager adds new price IDs, `_clearRemovedPriceUpdates` deletes stored data for removed feeds and `priceLastUpdatedAt` is reset to `0`. The new feed has no stored data. Any keeper can immediately call `updatePriceFeeds` — the loop hits the new feed first, returns immediately, and the update is accepted for **all** feeds (including existing ones whose prices have not deviated at all). [3](#0-2) 

The `TimestampOlderThanLastUpdate` guard that could otherwise block rapid re-updates is also gated on `priceLastUpdatedAt > 0`, so after the reset it provides no protection either:

```solidity
if (
    status.priceLastUpdatedAt > 0 &&
    updateTimestamp <= status.priceLastUpdatedAt
) {
    revert SchedulerErrors.TimestampOlderThanLastUpdate(...);
}
``` [4](#0-3) 

The attacker entry point is `updatePriceFeeds`, which is permissionless — no registration or privileged role is required:

> "Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`." [5](#0-4) 

### Impact Explanation

For a deviation-only subscription (e.g., `deviationThresholdBps = 100`, `updateOnHeartbeat = false`):

1. Manager creates subscription with feeds A, B, C. Baseline established on first update (expected).
2. Manager calls `updateSubscription` to add feed D. `priceLastUpdatedAt` resets to `0`; feed D has no stored data.
3. Keeper immediately calls `updatePriceFeeds`. Feed D triggers `previousFeed.id == bytes32(0)` → function returns immediately.
4. All four feeds (A, B, C, D) are updated and stored. Keeper collects full keeper fees for all feeds.
5. Prices for A, B, C have not moved at all — the 1% deviation threshold the manager configured provided zero protection.

The subscription manager loses one extra update's worth of keeper fees per `updateSubscription` call that adds new price IDs. The deviation threshold — the sole rate-limiting mechanism for a deviation-only subscription — is silently void at every such event. For subscriptions with many price IDs and high keeper fees, this is a meaningful financial loss.

### Likelihood Explanation

- `updatePriceFeeds` is callable by any address with no access control.
- The bypass condition (`previousFeed.id == bytes32(0)`) is deterministically reachable after every `updateSubscription` that adds new price IDs — a normal, expected manager action.
- Economically motivated keepers have direct incentive to exploit this: they collect fees for an update that would otherwise be rejected.
- No special knowledge, leaked keys, or privileged access is required.

### Recommendation

Instead of returning immediately for the entire function when one feed has no baseline, treat the missing-baseline feed as "deviation met for this feed" and continue evaluating the remaining feeds. Only allow the update if at least one feed either has no baseline or has genuine deviation:

```solidity
if (params.updateCriteria.updateOnDeviation) {
    bool deviationMet = false;
    for (uint8 i = 0; i < priceFeeds.length; i++) {
        PythStructs.PriceFeed storage previousFeed = _state
            .priceUpdates[subscriptionId][priceFeeds[i].id];

        if (previousFeed.id == bytes32(0)) {
            // New feed — baseline not yet established; counts as deviation met
            deviationMet = true;
            continue; // do NOT return; keep checking other feeds
        }

        int64 currentPrice  = priceFeeds[i].price.price;
        int64 previousPrice = previousFeed.price.price;

        if (previousPrice == 0 || currentPrice == 0) {
            continue;
        }

        uint256 deviationBps = Math.mulDiv(
            SignedMath.abs(currentPrice - previousPrice),
            10_000,
            SignedMath.abs(previousPrice)
        );

        if (deviationBps >= params.updateCriteria.deviationThresholdBps) {
            deviationMet = true;
        }
    }
    if (deviationMet) return updateTimestamp;
}
```

This ensures that existing feeds are still subject to the configured deviation threshold even when a new feed is added to the subscription.

### Proof of Concept

```solidity
// Deviation-only subscription, 1% threshold, no heartbeat.
// After manager adds a new price ID, keeper drains funds without any price movement.

function testDeviationBypassOnNewPriceId() public {
    // 1. Create deviation-only subscription (1% threshold)
    SchedulerStructs.UpdateCriteria memory criteria = SchedulerStructs.UpdateCriteria({
        updateOnHeartbeat: false,
        heartbeatSeconds: 0,
        updateOnDeviation: true,
        deviationThresholdBps: 100  // 1%
    });
    uint256 subId = addTestSubscriptionWithUpdateCriteria(scheduler, criteria, address(reader));
    scheduler.addFunds{value: 2 ether}(subId);

    // 2. First update — establishes baseline for feeds A, B
    uint64 t1 = SafeCast.toUint64(block.timestamp);
    (PythStructs.PriceFeed[] memory feeds1, uint64[] memory slots1) =
        createMockPriceFeedsWithSlots(t1, 2);
    mockParsePriceFeedUpdatesWithSlotsStrict(pyth, feeds1, slots1);
    vm.prank(pusher);
    scheduler.updatePriceFeeds(subId, createMockUpdateData(feeds1));

    // 3. Manager adds feed C — priceLastUpdatedAt reset to 0
    (SchedulerStructs.SubscriptionParams memory p,) = scheduler.getSubscription(subId);
    bytes32[] memory newIds = new bytes32[](3);
    newIds[0] = p.priceIds[0]; newIds[1] = p.priceIds[1];
    newIds[2] = keccak256("NEW_FEED_C");
    p.priceIds = newIds;
    scheduler.updateSubscription(subId, p);

    // 4. Prices for A and B have NOT moved (0 bps deviation).
    //    Feed C has no baseline. Keeper calls updatePriceFeeds immediately.
    vm.warp(block.timestamp + 1);
    uint64 t2 = SafeCast.toUint64(block.timestamp);
    // feeds2 uses SAME prices as feeds1 for A and B — zero deviation
    (PythStructs.PriceFeed[] memory feeds2, uint64[] memory slots2) =
        createMockPriceFeedsWithSlots(t2, 3);
    feeds2[0].price.price = feeds1[0].price.price; // A: unchanged
    feeds2[1].price.price = feeds1[1].price.price; // B: unchanged
    mockParsePriceFeedUpdatesWithSlotsStrict(pyth, feeds2, slots2);

    uint256 keeperBalBefore = pusher.balance;

    // EXPECTED (if check worked): revert UpdateConditionsNotMet — A and B haven't deviated
    // ACTUAL: succeeds — feed C has no baseline, bypasses check for A and B
    vm.prank(pusher);
    scheduler.updatePriceFeeds(subId, createMockUpdateData(feeds2));

    // Keeper collected fees even though A and B never moved
    assertGt(pusher.balance, keeperBalBefore, "Keeper paid despite zero deviation on A and B");
}
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L61-66)
```text
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        status.priceLastUpdatedAt = 0;
        status.balanceInWei = msg.value;
        status.totalUpdates = 0;
        status.totalSpent = 0;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L137-147)
```text
        // Clear price updates for removed price IDs before updating params
        bool newPriceIdsAdded = _clearRemovedPriceUpdates(
            subscriptionId,
            currentParams.priceIds,
            newParams.priceIds
        );

        // Reset priceLastUpdatedAt to 0 if new price IDs were added
        if (newPriceIdsAdded) {
            _state.subscriptionStatuses[subscriptionId].priceLastUpdatedAt = 0;
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L389-397)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L412-422)
```text
        // If updateOnDeviation is enabled, check if any price has deviated enough
        if (params.updateCriteria.updateOnDeviation) {
            for (uint8 i = 0; i < priceFeeds.length; i++) {
                // Get the previous price feed for this price ID using subscriptionId
                PythStructs.PriceFeed storage previousFeed = _state
                    .priceUpdates[subscriptionId][priceFeeds[i].id];

                // If there's no previous price, this is the first update
                if (previousFeed.id == bytes32(0)) {
                    return updateTimestamp;
                }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L60-62)
```markdown
- Anyone can run a Keeper node; no registration is required to call `updatePriceFeeds`. The main goal of making this component a permissionless network rather a set of permissioned nodes is to enhance reliability for the feeds -- if one provider fails, others should be available to service the subscriptions. We can improve this reliability by sourcing independent providers, and by making it profitable to push updates, paid out by the users of the feeds.

- Keepers are paid directly by the subscription's funds held in this contract for each successful update they perform. The payment covers gas costs plus a premium, and payment is sent directly to `msg.sender` (the keeper) at the end of `updatePriceFeeds`. The first transaction included in a block that passes checks will succeed and receive the payment. Subsequent attempts for the same update interval will revert since we verify the update criteria on-chain. By only allowing updates when they are needed, we keep costs predictable for the users.
```
