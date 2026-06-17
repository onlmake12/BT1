### Title
Stale `priceUpdates` Not Cleared When Price IDs Are Modified While Subscription Is Inactive — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

In `Scheduler.sol`, the `updateSubscription` function contains an early-return path for the case where a subscription is inactive and will remain inactive. This path skips the `_clearRemovedPriceUpdates` call and does not reset `priceLastUpdatedAt`. As a result, stale `priceUpdates` storage entries for removed price IDs persist indefinitely. Because `_getPricesInternal` does not validate that a requested price ID is currently registered in the subscription's params, any whitelisted reader (or any caller when `whitelistEnabled = false`) can later retrieve the stale price data through `getPricesUnsafe` / `getPricesNoOlderThan`, bypassing the intended freshness guarantee.

---

### Finding Description

`updateSubscription` handles the inactive-to-inactive case with an early return:

```solidity
// Scheduler.sol lines 97–102
if (!wasActive && !willBeActive) {
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;
}
```

This path overwrites `subscriptionParams` with `newParams` (which may have a different `priceIds` array) but **never calls `_clearRemovedPriceUpdates`** and **never resets `priceLastUpdatedAt`**.

The active-path (lines 137–147) does both:

```solidity
bool newPriceIdsAdded = _clearRemovedPriceUpdates(
    subscriptionId, currentParams.priceIds, newParams.priceIds
);
if (newPriceIdsAdded) {
    _state.subscriptionStatuses[subscriptionId].priceLastUpdatedAt = 0;
}
```

When the subscription is later reactivated, `_clearRemovedPriceUpdates` is called comparing the **post-inactive-update** params (e.g., `[A, B]`) against the reactivation params (e.g., `[A, B]`). Because the removed price ID `C` is already absent from `currentParams.priceIds` at that point, it is never detected as "removed" and its storage entry is never deleted.

`_getPricesInternal` (lines 500–510) reads directly from `_state.priceUpdates[subscriptionId][priceIds[i]]` without checking whether `priceIds[i]` is present in `params.priceIds`:

```solidity
PythStructs.PriceFeed memory priceFeed = _state.priceUpdates[subscriptionId][priceIds[i]];
if (priceFeed.id == bytes32(0)) {
    revert SchedulerErrors.InvalidPriceId(priceIds[i], bytes32(0));
}
requestedFeeds[i] = priceFeed;
```

`getPricesNoOlderThan` (lines 546–554) uses the subscription-level `priceLastUpdatedAt` as its sole freshness gate:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

After the subscription is reactivated and `updatePriceFeeds` is called for `[A, B]`, `priceLastUpdatedAt` is set to a recent timestamp `T2`. The freshness check then passes for any price ID whose storage slot is non-zero — including the stale entry for `C` (last written at `T1`).

---

### Impact Explanation

A caller (whitelisted reader, or any address when `whitelistEnabled = false`) can invoke `getPricesNoOlderThan(subscriptionId, [C], age)` and receive price data for `C` that was last written during the subscription's previous active period. The `getPricesNoOlderThan` freshness check passes because it tests `priceLastUpdatedAt` (which reflects the recent `[A, B]` update), not the individual price feed's `publishTime`. Downstream consumers that rely on `getPricesNoOlderThan` as a correctness guarantee will silently receive arbitrarily stale prices for a feed the subscription manager intended to remove, potentially causing incorrect financial settlements.

---

### Likelihood Explanation

The trigger sequence is:
1. Subscription active with `[A, B, C]`, prices updated recently.
2. Manager deactivates the subscription.
3. Manager calls `updateSubscription` (inactive → inactive) removing `C` → early return, no cleanup.
4. Manager reactivates with `[A, B]` (no new IDs added → `priceLastUpdatedAt` not reset).
5. Keeper calls `updatePriceFeeds` → `priceLastUpdatedAt = T2` (recent).
6. Any reader calls `getPricesNoOlderThan(subscriptionId, [C], largeAge)` → stale data returned.

This is a realistic operational pattern (deactivate → trim feeds → reactivate). The manager may not be aware that the inactive-update path skips cleanup. Likelihood is **medium**.

---

### Recommendation

Remove the early-return shortcut for the inactive-to-inactive case, or replicate the cleanup logic inside it:

```solidity
if (!wasActive && !willBeActive) {
    // Clear stale price data for any removed price IDs
    bool newPriceIdsAdded = _clearRemovedPriceUpdates(
        subscriptionId,
        currentParams.priceIds,
        newParams.priceIds
    );
    if (newPriceIdsAdded) {
        _state.subscriptionStatuses[subscriptionId].priceLastUpdatedAt = 0;
    }
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;
}
```

Additionally, `_getPricesInternal` should validate that each requested price ID is present in `params.priceIds` to enforce subscription boundaries as a defense-in-depth measure.

---

### Proof of Concept

1. Deploy `SchedulerUpgradable` and register a subscription with `priceIds = [A, B, C]`, funded above minimum balance.
2. Call `updatePriceFeeds` to populate `priceUpdates[subId][A]`, `priceUpdates[subId][B]`, `priceUpdates[subId][C]` and set `priceLastUpdatedAt = T1`.
3. Call `updateSubscription(subId, params_with_isActive=false)` → subscription deactivated.
4. Call `updateSubscription(subId, params_with_priceIds=[A,B]_isActive=false)` → **early return fires**, `priceUpdates[subId][C]` is NOT deleted, `priceLastUpdatedAt` stays `T1`.
5. Call `updateSubscription(subId, params_with_priceIds=[A,B]_isActive=true)` → reactivated; `newPriceIdsAdded = false` (no new IDs vs current `[A,B]`), so `priceLastUpdatedAt` is NOT reset.
6. Call `updatePriceFeeds` for `[A, B]` → `priceLastUpdatedAt = T2` (recent).
7. Call `getPricesNoOlderThan(subId, [C], 3600)` → freshness check passes (`distance(now, T2) < 3600`), returns stale price data for `C` from step 2. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L97-102)
```text
        if (!wasActive && !willBeActive) {
            // Update subscription parameters
            _state.subscriptionParams[subscriptionId] = newParams;
            emit SubscriptionUpdated(subscriptionId);
            return;
        }
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L228-273)
```text
    function _clearRemovedPriceUpdates(
        uint256 subscriptionId,
        bytes32[] storage currentPriceIds,
        bytes32[] memory newPriceIds
    ) internal returns (bool newPriceIdsAdded) {
        // Iterate through old price IDs
        for (uint i = 0; i < currentPriceIds.length; i++) {
            bytes32 oldPriceId = currentPriceIds[i];
            bool found = false;

            // Check if the old price ID exists in the new list
            for (uint j = 0; j < newPriceIds.length; j++) {
                if (newPriceIds[j] == oldPriceId) {
                    found = true;
                    break; // Found it, no need to check further
                }
            }

            // If not found in the new list, delete its stored update data
            if (!found) {
                delete _state.priceUpdates[subscriptionId][oldPriceId];
            }
        }

        // Check if any new price IDs were added
        for (uint i = 0; i < newPriceIds.length; i++) {
            bytes32 newPriceId = newPriceIds[i];
            bool found = false;

            // Check if the new price ID exists in the current list
            for (uint j = 0; j < currentPriceIds.length; j++) {
                if (currentPriceIds[j] == newPriceId) {
                    found = true;
                    break;
                }
            }

            // If a new price ID was added, mark as changed
            if (!found) {
                newPriceIdsAdded = true;
                break;
            }
        }

        return newPriceIdsAdded;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L496-510)
```text
        PythStructs.PriceFeed[]
            memory requestedFeeds = new PythStructs.PriceFeed[](
                priceIds.length
            );
        for (uint8 i = 0; i < priceIds.length; i++) {
            PythStructs.PriceFeed memory priceFeed = _state.priceUpdates[
                subscriptionId
            ][priceIds[i]];

            // Check if the price feed exists (price ID is valid and has been updated)
            if (priceFeed.id == bytes32(0)) {
                revert SchedulerErrors.InvalidPriceId(priceIds[i], bytes32(0));
            }
            requestedFeeds[i] = priceFeed;
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L546-555)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
    }
```
