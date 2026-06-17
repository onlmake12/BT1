### Title
`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` Always Revert for Whitelisted Readers When Whitelist Is Enabled - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` in `Scheduler.sol` use `this.<function>(...)` to call `getPricesUnsafe` / `getEmaPricesUnsafe` as external calls. Both the outer and inner functions carry the `onlyWhitelistedReader` modifier, which checks `msg.sender`. When the outer function calls the inner via `this.`, `msg.sender` inside the inner call becomes `address(this)` (the Scheduler contract itself), not the original caller. The Scheduler contract is never in any subscription's whitelist, so the inner call always reverts when `whitelistEnabled = true`, making both "no older than" functions permanently broken for whitelisted readers.

---

### Finding Description

`getPricesNoOlderThan` passes the `onlyWhitelistedReader` check for the original caller, then delegates to `getPricesUnsafe` via an external `this.` call:

```solidity
// Scheduler.sol line 554
prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

`getPricesUnsafe` is also gated by `onlyWhitelistedReader`:

```solidity
function getPricesUnsafe(...)
    external view override
    onlyWhitelistedReader(subscriptionId)   // ← re-checked with msg.sender = address(this)
    returns (PythStructs.Price[] memory prices)
```

The modifier logic:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // check whitelist array for msg.sender ...
    if (!isWhitelisted) { revert SchedulerErrors.Unauthorized(); }
    _;
}
```

When `whitelistEnabled = true`, `msg.sender` inside `getPricesUnsafe` is `address(this)` (the Scheduler proxy). The Scheduler contract is never the subscription manager and is never added to any `readerWhitelist`, so the modifier always reverts. The identical pattern exists in `getEmaPricesNoOlderThan` → `this.getEmaPricesUnsafe(...)`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

---

### Impact Explanation

Any subscription that enables `whitelistEnabled = true` (the privacy feature explicitly designed for restricted-access subscriptions) renders `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` permanently non-functional for all callers, including the subscription manager. Whitelisted readers who need recency-checked prices — the primary safe API recommended by the SDK — receive only reverts. They are forced to fall back to the unsafe variants and perform manual staleness checks, defeating the purpose of the safer API. The subscription manager cannot work around this without disabling the whitelist entirely, sacrificing access control.

---

### Likelihood Explanation

Medium. The whitelist feature is an explicitly documented, user-configurable option. Any subscription manager who enables `whitelistEnabled = true` and whose consumers call `getPricesNoOlderThan` or `getEmaPricesNoOlderThan` will trigger the revert. No special attacker action is required — the bug is triggered by normal, intended usage of the contract's own API.

---

### Recommendation

Replace the external `this.` calls with direct calls to the internal helper `_getPricesInternal`, bypassing the redundant `onlyWhitelistedReader` re-check (the outer function already enforces it):

```solidity
// getPricesNoOlderThan
prices_raw = _getPricesInternal(subscriptionId, priceIds);
// then extract .price fields

// getEmaPricesNoOlderThan
prices_raw = _getPricesInternal(subscriptionId, priceIds);
// then extract .emaPrice fields
```

This mirrors how `getPricesUnsafe` and `getEmaPricesUnsafe` already call `_getPricesInternal` directly.

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription with `whitelistEnabled = true` and `readerWhitelist = [alice]`.
2. Fund the subscription and push a price update via `updatePriceFeeds`.
3. Call `getPricesNoOlderThan(subscriptionId, priceIds, 60)` as `alice` (a whitelisted reader).
4. The outer `onlyWhitelistedReader` passes (alice is whitelisted).
5. `this.getPricesUnsafe(subscriptionId, priceIds)` is dispatched; inside that call `msg.sender == address(scheduler)`.
6. `onlyWhitelistedReader` runs again: scheduler is not the manager, whitelist is enabled, scheduler is not in the whitelist → `revert SchedulerErrors.Unauthorized()`.
7. `alice`'s call reverts despite being a legitimately whitelisted reader. [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L535-555)
```text
    function getPricesNoOlderThan(
        uint256 subscriptionId,
        bytes32[] calldata priceIds,
        uint256 age_seconds
    )
        external
        view
        override
        onlyWhitelistedReader(subscriptionId)
        returns (PythStructs.Price[] memory prices)
    {
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L578-598)
```text
    function getEmaPricesNoOlderThan(
        uint256 subscriptionId,
        bytes32[] calldata priceIds,
        uint256 age_seconds
    )
        external
        view
        override
        onlyWhitelistedReader(subscriptionId)
        returns (PythStructs.Price[] memory prices)
    {
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L750-779)
```text
    modifier onlyWhitelistedReader(uint256 subscriptionId) {
        // Manager is always allowed
        if (_state.subscriptionManager[subscriptionId] == msg.sender) {
            _;
            return;
        }

        // If whitelist is not used, allow any reader
        if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) {
            _;
            return;
        }

        // Check if caller is in whitelist
        address[] storage whitelist = _state
            .subscriptionParams[subscriptionId]
            .readerWhitelist;
        bool isWhitelisted = false;
        for (uint i = 0; i < whitelist.length; i++) {
            if (whitelist[i] == msg.sender) {
                isWhitelisted = true;
                break;
            }
        }

        if (!isWhitelisted) {
            revert SchedulerErrors.Unauthorized();
        }
        _;
    }
```
