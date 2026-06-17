### Title
`onlyWhitelistedReader` Incomplete Authorization Causes `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` to Always Revert for Whitelisted Subscriptions - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` in `Scheduler.sol` delegate to `getPricesUnsafe` / `getEmaPricesUnsafe` via an external `this.` call. Because `this.fn()` in Solidity is an external call, `msg.sender` inside the callee becomes `address(this)` (the Scheduler contract itself). The `onlyWhitelistedReader` modifier does not recognize the Scheduler contract as a valid caller, so it reverts with `Unauthorized` for every subscription that has `whitelistEnabled = true`, regardless of who the original caller was.

---

### Finding Description

`getPricesNoOlderThan` applies `onlyWhitelistedReader` to authorize the original caller, then delegates to `getPricesUnsafe` via `this.getPricesUnsafe(...)`:

```solidity
// Scheduler.sol line 554
prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

`getPricesUnsafe` is declared `external` and also carries `onlyWhitelistedReader`:

```solidity
// Scheduler.sol line 514-521
function getPricesUnsafe(
    uint256 subscriptionId,
    bytes32[] calldata priceIds
)
    external
    view
    override
    onlyWhitelistedReader(subscriptionId)
```

The `onlyWhitelistedReader` modifier checks three paths:

```solidity
// Scheduler.sol line 750-779
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // linear scan of readerWhitelist ...
    if (!isWhitelisted) { revert SchedulerErrors.Unauthorized(); }
    _;
}
```

When `getPricesNoOlderThan` calls `this.getPricesUnsafe(...)`, the EVM sets `msg.sender = address(this)` inside `getPricesUnsafe`. The Scheduler contract address is never the `subscriptionManager` and is never in `readerWhitelist`. Therefore, when `whitelistEnabled = true`, the modifier always reverts. The same flaw exists in `getEmaPricesNoOlderThan` → `this.getEmaPricesUnsafe(...)`.

The analog to M-19 is exact: just as `_validateCommitment` checked `approve()` but not `setApprovalForAll()`, `onlyWhitelistedReader` checks the manager and the explicit whitelist but not the implicit authorization path of the contract calling itself on behalf of an already-authorized user.

---

### Impact Explanation

Any subscription created with `whitelistEnabled = true` renders `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` permanently broken — they revert for every caller, including the subscription manager. These are the staleness-checked, production-safe price reading functions. Subscribers who enable the whitelist feature (the security-conscious ones) lose access to the safer API entirely and are forced to fall back to `getPricesUnsafe`, defeating the purpose of the staleness guard.

---

### Likelihood Explanation

Any user who creates a subscription and sets `whitelistEnabled = true` in `SubscriptionParams` immediately triggers this. It requires no special privilege, no key compromise, and no adversarial action — a normal subscriber exercising a documented feature of the contract hits the bug on the first call to `getPricesNoOlderThan`.

---

### Recommendation

Replace the external `this.` delegation with a direct call to the internal helper `_getPricesInternal`, which already exists and performs the same logic without re-entering the access-control layer:

```solidity
// getPricesNoOlderThan
prices = new PythStructs.Price[](...);
PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
for (uint i = 0; i < priceFeeds.length; i++) {
    prices[i] = priceFeeds[i].price;
}

// getEmaPricesNoOlderThan
PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
for (uint i = 0; i < priceFeeds.length; i++) {
    prices[i] = priceFeeds[i].emaPrice;
}
```

This mirrors the pattern already used inside `getPricesUnsafe` and `getEmaPricesUnsafe` themselves.

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable`.
2. Call `createSubscription` with `whitelistEnabled = true` and `readerWhitelist = [alice]`.
3. Fund the subscription and trigger a `updatePriceFeeds` to populate price data.
4. As `alice` (a whitelisted reader), call `getPricesNoOlderThan(subscriptionId, priceIds, 3600)`.
5. Observe revert with `SchedulerErrors.Unauthorized`.

Trace:
- `getPricesNoOlderThan` → `onlyWhitelistedReader`: `alice` is in whitelist → passes.
- `this.getPricesUnsafe(subscriptionId, priceIds)` → external call, `msg.sender = address(scheduler)`.
- `onlyWhitelistedReader` inside `getPricesUnsafe`: `address(scheduler)` is not manager, whitelist is enabled, `address(scheduler)` is not in whitelist → **reverts**.

Relevant lines: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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
