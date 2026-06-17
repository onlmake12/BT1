### Title
`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` Permanently DoS for Whitelist-Enabled Subscriptions via `this.` Internal Call Pattern — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` in `Scheduler.sol` delegate to `getPricesUnsafe` / `getEmaPricesUnsafe` using `this.<fn>(...)` external calls. This makes `msg.sender == address(this)` (the Scheduler contract itself) inside the callee. The `onlyWhitelistedReader` modifier on those callees then rejects the call for every whitelist-enabled subscription, because `address(this)` is neither the subscription manager nor a whitelisted reader. The result is a permanent, unconditional revert of both time-bounded price-read functions for any subscription that has `whitelistEnabled = true`.

---

### Finding Description

`getPricesNoOlderThan` is declared `external` and carries the `onlyWhitelistedReader` modifier. After its own staleness check it calls:

```solidity
prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [1](#0-0) 

`getPricesUnsafe` is also `external` and also carries `onlyWhitelistedReader`:

```solidity
function getPricesUnsafe(
    uint256 subscriptionId,
    bytes32[] calldata priceIds
)
    external
    view
    override
    onlyWhitelistedReader(subscriptionId)
``` [2](#0-1) 

The `onlyWhitelistedReader` modifier evaluates `msg.sender` in three steps:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // ... check whitelist array ...
    if (!isWhitelisted) { revert SchedulerErrors.Unauthorized(); }
    _;
}
``` [3](#0-2) 

When `getPricesNoOlderThan` issues `this.getPricesUnsafe(...)`, the EVM dispatches an external call back to the contract; inside that call `msg.sender` is `address(this)` — the Scheduler contract itself. For any subscription where `whitelistEnabled = true`:

1. `address(this)` ≠ `subscriptionManager` → first branch skipped.
2. `whitelistEnabled == true` → second branch skipped.
3. `address(this)` is not in `readerWhitelist` → `revert SchedulerErrors.Unauthorized()`.

The identical pattern exists in `getEmaPricesNoOlderThan`, which calls `this.getEmaPricesUnsafe(...)`: [4](#0-3) 

---

### Impact Explanation

Any subscription created with `whitelistEnabled = true` loses access to both `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` entirely — even for the subscription manager and every legitimately whitelisted address. These are the only functions that enforce a recency guarantee on returned prices; their permanent unavailability forces consumers to fall back to `getPricesUnsafe` / `getEmaPricesUnsafe`, which carry no staleness protection. A DeFi protocol relying on the time-bounded variants for safe price consumption would silently receive stale data or be forced to revert all price-dependent operations.

---

### Likelihood Explanation

The whitelist feature is a first-class, documented capability of the Scheduler. Any integrator that enables it to restrict price-read access (the intended use case) will immediately and permanently trigger this DoS on every call to `getPricesNoOlderThan` / `getEmaPricesNoOlderThan`. No attacker action is required; the bug is self-activating upon subscription creation with `whitelistEnabled = true`.

---

### Recommendation

Refactor `getPricesUnsafe` and `getEmaPricesUnsafe` to expose an `internal` helper that performs the actual price lookup without the modifier, and have the `external` entry points apply `onlyWhitelistedReader` themselves. `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` should then call the internal helper directly, bypassing the re-entrant `this.` dispatch entirely.

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable`.
2. Call `createSubscription` with `whitelistEnabled = true` and `readerWhitelist = [alice]`.
3. As `alice` (a whitelisted reader), call `getPricesNoOlderThan(subscriptionId, priceIds, 300)`.
4. Observe unconditional revert with `SchedulerErrors.Unauthorized` — the inner `this.getPricesUnsafe(...)` call sees `msg.sender == address(scheduler)`, which is neither the manager nor in the whitelist.
5. Confirm `getPricesUnsafe` called directly by `alice` succeeds, proving the whitelist is correctly configured and the failure is solely due to the `this.` dispatch. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L514-555)
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L750-778)
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
```
