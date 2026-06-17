### Title
New Price IDs Added via `updateSubscription()` Cause Permanent `InvalidPriceId` Revert in `_getPricesInternal()` Until Keeper Update — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

When a subscription manager adds new price IDs via `updateSubscription()`, the new IDs have no stored price data (`priceUpdates[subscriptionId][newId].id == bytes32(0)`). `_getPricesInternal()` iterates over **all** subscription price IDs and unconditionally reverts with `InvalidPriceId` for any uninitialized feed. This causes `getPricesUnsafe()`, `getEmaPricesUnsafe()`, `getPricesNoOlderThan()`, and `getEmaPricesNoOlderThan()` to revert for every reader requesting all prices, until a keeper successfully pushes an update for the full new feed set.

---

### Finding Description

**Step 1 — Partial state creation in `updateSubscription()`**

`updateSubscription()` calls `_clearRemovedPriceUpdates()`, which only deletes stored data for price IDs that were *removed* from the subscription. Price IDs that are *added* receive no stored price data — their mapping slot remains at the zero-value default (`PriceFeed.id == bytes32(0)`). [1](#0-0) 

The code also resets `priceLastUpdatedAt = 0` when new IDs are added, but does **not** initialize the new IDs' stored price entries. [2](#0-1) 

**Step 2 — Unconditional revert over all price IDs in `_getPricesInternal()`**

`_getPricesInternal()` iterates over every price ID in `params.priceIds` and reverts with `InvalidPriceId` the moment it encounters any entry whose stored `id` is `bytes32(0)`: [3](#0-2) 

There is no guard that distinguishes "price ID not in subscription" from "price ID in subscription but not yet updated." Both cases produce the same hard revert.

**Step 3 — All read paths are blocked**

Every public read function routes through `_getPricesInternal()`: [4](#0-3) [5](#0-4) 

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` additionally revert with `StalePrice` first (because `priceLastUpdatedAt` was reset to 0), but `getPricesUnsafe` and `getEmaPricesUnsafe` reach `_getPricesInternal()` directly and emit the misleading `InvalidPriceId` error.

---

### Impact Explanation

Any DeFi protocol or whitelisted reader that calls `getPricesUnsafe(subscriptionId, [])` (the "get all feeds" form) will receive a hard revert with `InvalidPriceId` immediately after the manager adds a new price ID. The subscription remains `isActive = true` and appears healthy from the outside, but all price reads fail. The DoS persists until a keeper successfully calls `updatePriceFeeds()` for the updated feed set. If the subscription uses `updateOnDeviation`-only criteria and the new feed's price does not deviate enough to trigger an update, the keeper may not push an update promptly, extending the outage. Protocols that cache the subscription's feed list and call `getPricesUnsafe` with an empty array are fully blocked.

---

### Likelihood Explanation

Adding price IDs to an existing subscription is a routine management operation explicitly supported by `updateSubscription()`. Any subscription manager — an unprivileged EOA or protocol governance contract — can trigger this condition. No special access beyond subscription ownership is required. The trigger is a single `updateSubscription()` call with one additional price ID.

---

### Recommendation

In `_getPricesInternal()`, distinguish between "price ID not registered in this subscription" (a true error) and "price ID registered but not yet updated" (a transient state). For the latter case, either skip the uninitialized entry or return a zero-valued `PriceFeed` rather than reverting. Alternatively, `updateSubscription()` should refuse to add new price IDs while the subscription is active, requiring the manager to deactivate, update, and reactivate — ensuring readers are never surprised by a mid-flight state change.

---

### Proof of Concept

```
1. Alice creates a subscription with priceIds = [A, B], funds it, and it becomes active.
2. A keeper calls updatePriceFeeds() — both A and B now have stored data.
   priceUpdates[subId][A].id = A  (non-zero)
   priceUpdates[subId][B].id = B  (non-zero)
3. A DeFi protocol (whitelisted reader) calls getPricesUnsafe(subId, []) successfully.
4. Alice calls updateSubscription() with priceIds = [A, B, C].
   _clearRemovedPriceUpdates() finds no removed IDs, sets newPriceIdsAdded = true.
   priceLastUpdatedAt is reset to 0.
   params.priceIds is now [A, B, C].
   priceUpdates[subId][C].id remains bytes32(0).
5. The DeFi protocol calls getPricesUnsafe(subId, []) again.
   _getPricesInternal() loops: A ok, B ok, C → id == bytes32(0) → revert InvalidPriceId(C, 0).
6. The protocol is now bricked until a keeper pushes an update containing C.
   If the subscription is deviation-only and C's price is stable, the keeper has no
   incentive to push, and the outage is indefinite.
``` [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L137-151)
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

        // Update subscription parameters
        _state.subscriptionParams[subscriptionId] = newParams;

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L462-512)
```text
    function _getPricesInternal(
        uint256 subscriptionId,
        bytes32[] calldata priceIds
    ) internal view returns (PythStructs.PriceFeed[] memory priceFeeds) {
        if (!_state.subscriptionParams[subscriptionId].isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        // If no price IDs provided, return all price feeds for the subscription
        if (priceIds.length == 0) {
            PythStructs.PriceFeed[]
                memory allFeeds = new PythStructs.PriceFeed[](
                    params.priceIds.length
                );
            for (uint8 i = 0; i < params.priceIds.length; i++) {
                PythStructs.PriceFeed memory priceFeed = _state.priceUpdates[
                    subscriptionId
                ][params.priceIds[i]];
                // Check if the price feed exists (price ID is valid and has been updated)
                if (priceFeed.id == bytes32(0)) {
                    revert SchedulerErrors.InvalidPriceId(
                        params.priceIds[i],
                        bytes32(0)
                    );
                }
                allFeeds[i] = priceFeed;
            }
            return allFeeds;
        }

        // Return only the requested price feeds
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
        return requestedFeeds;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L514-533)
```text
    function getPricesUnsafe(
        uint256 subscriptionId,
        bytes32[] calldata priceIds
    )
        external
        view
        override
        onlyWhitelistedReader(subscriptionId)
        returns (PythStructs.Price[] memory prices)
    {
        PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(
            subscriptionId,
            priceIds
        );
        prices = new PythStructs.Price[](priceFeeds.length);
        for (uint i = 0; i < priceFeeds.length; i++) {
            prices[i] = priceFeeds[i].price;
        }
        return prices;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L557-576)
```text
    function getEmaPricesUnsafe(
        uint256 subscriptionId,
        bytes32[] calldata priceIds
    )
        external
        view
        override
        onlyWhitelistedReader(subscriptionId)
        returns (PythStructs.Price[] memory prices)
    {
        PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(
            subscriptionId,
            priceIds
        );
        prices = new PythStructs.Price[](priceFeeds.length);
        for (uint i = 0; i < priceFeeds.length; i++) {
            prices[i] = priceFeeds[i].emaPrice;
        }
        return prices;
    }
```
