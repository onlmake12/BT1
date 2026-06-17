### Title
Duplicate `onlyWhitelistedReader` Modifier Application via External Self-Call Permanently Breaks `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` for Whitelist-Enabled Subscriptions - (File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol)

---

### Summary

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` in `Scheduler.sol` each carry the `onlyWhitelistedReader` modifier and then delegate to `this.getPricesUnsafe(...)` / `this.getEmaPricesUnsafe(...)` via an external `this.` call. Because `this.` triggers an EVM `CALL`, `msg.sender` inside the callee becomes `address(this)` (the Scheduler contract itself). The callee's own `onlyWhitelistedReader` modifier then checks whether the Scheduler contract is the subscription manager or is on the whitelist — it is neither — so the call always reverts with `Unauthorized` whenever `whitelistEnabled == true`.

---

### Finding Description

`getPricesNoOlderThan` is declared:

```solidity
function getPricesNoOlderThan(
    uint256 subscriptionId,
    bytes32[] calldata priceIds,
    uint256 age_seconds
)
    external view override
    onlyWhitelistedReader(subscriptionId)   // ← first application
    returns (PythStructs.Price[] memory prices)
{
    ...
    prices = this.getPricesUnsafe(subscriptionId, priceIds); // ← external self-call
}
``` [1](#0-0) 

`getPricesUnsafe` is declared:

```solidity
function getPricesUnsafe(
    uint256 subscriptionId,
    bytes32[] calldata priceIds
)
    external view override
    onlyWhitelistedReader(subscriptionId)   // ← second application, msg.sender = address(this)
    returns (PythStructs.Price[] memory prices)
``` [2](#0-1) 

The `onlyWhitelistedReader` modifier logic is:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // whitelist check against msg.sender ...
    if (!isWhitelisted) revert SchedulerErrors.Unauthorized();
    _;
}
``` [3](#0-2) 

When `getPricesNoOlderThan` calls `this.getPricesUnsafe(...)`, the EVM issues a new `CALL` opcode. Inside `getPricesUnsafe`, `msg.sender` is `address(this)` — the Scheduler proxy contract. The modifier then:

1. Checks `subscriptionManager[id] == address(this)` → **false** (the contract is never its own manager).
2. Checks `!whitelistEnabled` → **false** for whitelist-enabled subscriptions.
3. Checks whether `address(this)` is in the reader whitelist → **false**.
4. Reverts with `Unauthorized`.

The identical pattern exists in `getEmaPricesNoOlderThan` → `this.getEmaPricesUnsafe(...)`. [4](#0-3) 

---

### Impact Explanation

For any subscription created with `whitelistEnabled = true`, the functions `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` are permanently and unconditionally broken. Every call by any caller — including legitimately whitelisted readers and the subscription manager — reverts with `Unauthorized`. Subscribers who paid fees and configured a whitelist to protect their price data cannot use the staleness-checked read path at all. The only working read path is `getPricesUnsafe` / `getEmaPricesUnsafe`, which provides no age guarantee, defeating the purpose of the `NoOlderThan` variants entirely.

---

### Likelihood Explanation

The revert is deterministic: it triggers on 100% of calls to `getPricesNoOlderThan` / `getEmaPricesNoOlderThan` whenever `whitelistEnabled == true`. Any user who creates a subscription with a reader whitelist (a documented, supported feature) and then calls the staleness-checked read functions will hit this. No special attacker setup is required; a normal whitelisted reader exercising the advertised API is sufficient.

---

### Recommendation

Replace the external `this.` self-calls with direct internal calls to the shared helper `_getPricesInternal`, which already exists and contains the actual price-fetching logic without the access-control modifier:

```solidity
// getPricesNoOlderThan — replace:
//   prices = this.getPricesUnsafe(subscriptionId, priceIds);
// with:
PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
prices = new PythStructs.Price[](priceFeeds.length);
for (uint i = 0; i < priceFeeds.length; i++) {
    prices[i] = priceFeeds[i].price;
}
```

Apply the same fix to `getEmaPricesNoOlderThan`. This eliminates the redundant modifier application while preserving the staleness check and the single access-control gate already present on the outer function. [5](#0-4) 

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription with `whitelistEnabled = true` and one whitelisted reader address `alice`.
2. Have a keeper call `updatePriceFeeds` to populate price data.
3. Call `getPricesNoOlderThan(subscriptionId, priceIds, 60)` from `alice`.
4. Execution flow:
   - Outer `onlyWhitelistedReader`: `msg.sender == alice` → alice is whitelisted → passes.
   - Staleness check passes.
   - `this.getPricesUnsafe(subscriptionId, priceIds)` is issued as an external call.
   - Inner `onlyWhitelistedReader`: `msg.sender == address(Scheduler)` → not the manager, whitelist enabled, `address(Scheduler)` not in whitelist → **reverts `Unauthorized`**.
5. The transaction reverts despite `alice` being a legitimately whitelisted reader. [6](#0-5)

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
