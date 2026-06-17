### Title
`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` Always Revert for Whitelist-Enabled Subscriptions Due to Wrong `msg.sender` in Inner Authorization Check - (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol` contains two functions — `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` — that delegate to `this.getPricesUnsafe` and `this.getEmaPricesUnsafe` via **external** calls. Because these are external calls, `msg.sender` inside the inner function becomes `address(this)` (the Scheduler contract itself), not the original caller. The `onlyWhitelistedReader` modifier in the inner function then checks `address(this)` against the stored manager and whitelist, causing an unconditional `Unauthorized` revert for any subscription with `whitelistEnabled = true`.

---

### Finding Description

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` are guarded by `onlyWhitelistedReader` and correctly validate the original caller. However, they then forward execution to `getPricesUnsafe` / `getEmaPricesUnsafe` via `this.X(...)` — an external call:

```solidity
// Scheduler.sol L535-L555
function getPricesNoOlderThan(...) external view override
    onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    ...
    prices = this.getPricesUnsafe(subscriptionId, priceIds); // external call
}
``` [1](#0-0) 

```solidity
// Scheduler.sol L578-L598
function getEmaPricesNoOlderThan(...) external view override
    onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    ...
    prices = this.getEmaPricesUnsafe(subscriptionId, priceIds); // external call
}
``` [2](#0-1) 

Because `this.X(...)` is an EVM external call, `msg.sender` inside `getPricesUnsafe` / `getEmaPricesUnsafe` is `address(this)`, not the original caller. Both inner functions carry the same `onlyWhitelistedReader` modifier:

```solidity
// Scheduler.sol L750-L779
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { // address(this) ≠ manager
        _; return;
    }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { // if enabled → no short-circuit
        _; return;
    }
    // address(this) is never in the whitelist
    ...
    if (!isWhitelisted) {
        revert SchedulerErrors.Unauthorized(); // always reverts
    }
    _;
}
``` [3](#0-2) 

When `whitelistEnabled = true`, `address(this)` is neither the manager nor a whitelisted reader, so the inner call always reverts. The outer authorization check (which correctly validated the original caller) is rendered useless.

An internal helper `_getPricesInternal` already exists and is called by `getPricesUnsafe` — the outer functions should call it directly instead:

```solidity
// Scheduler.sol L514-L532
function getPricesUnsafe(...) external view override
    onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
    ...
}
``` [4](#0-3) 

---

### Impact Explanation

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` are completely non-functional for any subscription with `whitelistEnabled = true`. This includes the subscription manager themselves. Subscribers who pay to use the Scheduler's price feed service and enable the whitelist to restrict access cannot use the freshness-checked price retrieval functions at all — a core feature of the contract is broken.

---

### Likelihood Explanation

The whitelist feature (`whitelistEnabled`) is an explicit, documented subscription parameter. Any subscription that enables it to restrict price data access to specific readers will trigger this bug on every call to `getPricesNoOlderThan` or `getEmaPricesNoOlderThan`. No special attacker action is required — the bug is triggered by normal, intended usage.

---

### Recommendation

Replace the external `this.X(...)` calls with direct calls to the internal helper `_getPricesInternal` (and the equivalent EMA path), bypassing the redundant re-authorization:

```diff
function getPricesNoOlderThan(...) external view override
    onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    ...
-   prices = this.getPricesUnsafe(subscriptionId, priceIds);
+   PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
+   prices = new PythStructs.Price[](priceFeeds.length);
+   for (uint i = 0; i < priceFeeds.length; i++) { prices[i] = priceFeeds[i].price; }
}

function getEmaPricesNoOlderThan(...) external view override
    onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    ...
-   prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
+   PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
+   prices = new PythStructs.Price[](priceFeeds.length);
+   for (uint i = 0; i < priceFeeds.length; i++) { prices[i] = priceFeeds[i].emaPrice; }
}
```

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable` and create a subscription with `whitelistEnabled = true` and a non-empty `readerWhitelist` containing `alice`.
2. As `alice` (a whitelisted reader), call `getPricesNoOlderThan(subscriptionId, priceIds, 60)`.
3. The outer `onlyWhitelistedReader` passes (alice is whitelisted).
4. `this.getPricesUnsafe(subscriptionId, priceIds)` is called externally; `msg.sender` inside is `address(this)`.
5. The inner `onlyWhitelistedReader` checks `address(this)` — not the manager, whitelist is enabled, `address(this)` not in whitelist → **reverts with `Unauthorized`**.
6. Repeat with the subscription manager as caller — same revert.
7. Confirm that calling `getPricesUnsafe` directly as `alice` succeeds (the outer check works correctly in isolation). [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L514-598)
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
