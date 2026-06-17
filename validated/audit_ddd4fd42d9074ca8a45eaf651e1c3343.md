### Title
`Scheduler.getPricesNoOlderThan` and `getEmaPricesNoOlderThan` Always Revert for Whitelisted Subscriptions Due to `this.` External Call Re-Checking Access Control - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` are gated by `onlyWhitelistedReader`, which permits the subscription manager, any reader (if whitelist is disabled), or explicitly whitelisted addresses. However, both functions internally call `this.getPricesUnsafe(...)` and `this.getEmaPricesUnsafe(...)` respectively using an **external** `this.` call. This causes `msg.sender` inside the callee to become `address(this)` (the Scheduler contract itself), which is neither the subscription manager nor a whitelisted reader. When `whitelistEnabled = true`, the `onlyWhitelistedReader` check in the callee always reverts, making both functions completely non-functional for any subscription that uses the whitelist feature.

### Finding Description

`getPricesNoOlderThan` passes the `onlyWhitelistedReader(subscriptionId)` modifier check for the original caller, then delegates to `this.getPricesUnsafe(subscriptionId, priceIds)`: [1](#0-0) 

`this.getPricesUnsafe(...)` is an external call. Inside `getPricesUnsafe`, `msg.sender` is now `address(this)` — the Scheduler contract — not the original caller: [2](#0-1) 

The `onlyWhitelistedReader` modifier checks:
1. Is `msg.sender` the subscription manager? → `address(this)` is not.
2. Is whitelist disabled? → If `whitelistEnabled = true`, this branch is skipped.
3. Is `msg.sender` in the whitelist? → `address(this)` is not. [3](#0-2) 

The identical pattern exists in `getEmaPricesNoOlderThan` → `this.getEmaPricesUnsafe(...)`: [4](#0-3) 

The root cause is that `_getPricesInternal` already exists as a shared internal helper, but `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` bypass it in favor of an external `this.` call that re-triggers the access control modifier with the wrong `msg.sender`: [5](#0-4) 

### Impact Explanation

Any subscription created with `whitelistEnabled = true` renders `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` permanently broken. Whitelisted readers — who are explicitly authorized — receive an `Unauthorized` revert every time they call these functions. The only working price-read path for such subscriptions is `getPricesUnsafe` / `getEmaPricesUnsafe`, which provides **no staleness guarantee**. Protocols relying on the staleness check for safety (e.g., to avoid acting on stale prices) are silently forced onto the unsafe path or are completely unable to read prices. This is a functional DoS on a core feature of the Scheduler contract.

### Likelihood Explanation

The whitelist feature (`whitelistEnabled`) is a first-class, documented feature of the Scheduler. Any integrator who enables it to restrict price-feed access to known addresses will immediately encounter this bug the first time they call `getPricesNoOlderThan` or `getEmaPricesNoOlderThan`. No special attacker is needed — any whitelisted reader triggers the revert simply by using the intended API. Likelihood is high for any deployment that uses the whitelist.

### Recommendation

Replace the external `this.getPricesUnsafe(...)` and `this.getEmaPricesUnsafe(...)` calls with direct calls to the internal helper `_getPricesInternal(...)`, which already exists for this purpose and does not re-check access control:

```solidity
// getPricesNoOlderThan — replace:
prices = this.getPricesUnsafe(subscriptionId, priceIds);
// with:
PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
prices = new PythStructs.Price[](priceFeeds.length);
for (uint i = 0; i < priceFeeds.length; i++) { prices[i] = priceFeeds[i].price; }

// getEmaPricesNoOlderThan — replace:
prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
// with:
PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
prices = new PythStructs.Price[](priceFeeds.length);
for (uint i = 0; i < priceFeeds.length; i++) { prices[i] = priceFeeds[i].emaPrice; }
```

### Proof of Concept

1. Deploy `SchedulerUpgradeable`.
2. Call `createSubscription` with `whitelistEnabled = true` and `readerWhitelist = [alice]`.
3. As `alice` (a whitelisted reader), call `getPricesNoOlderThan(subscriptionId, priceIds, 60)`.
4. Execution flow:
   - `onlyWhitelistedReader` passes for `alice` ✓
   - `this.getPricesUnsafe(subscriptionId, priceIds)` is called externally
   - Inside `getPricesUnsafe`, `msg.sender == address(Scheduler)` — not `alice`
   - `onlyWhitelistedReader` checks: manager? No. Whitelist disabled? No. `address(Scheduler)` in whitelist? No.
   - Reverts with `SchedulerErrors.Unauthorized` ✗
5. The same revert occurs for `getEmaPricesNoOlderThan`. [6](#0-5) [7](#0-6)

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
