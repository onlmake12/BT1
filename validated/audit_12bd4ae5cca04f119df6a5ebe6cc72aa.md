### Title
`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` Always Revert for Whitelist-Enabled Subscriptions Due to `this.` External Self-Call Changing `msg.sender` - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getPricesNoOlderThan` and `Scheduler.getEmaPricesNoOlderThan` internally call `this.getPricesUnsafe(...)` and `this.getEmaPricesUnsafe(...)` respectively using an external `this.` dispatch. This changes `msg.sender` to the contract's own address for the inner call. Both `getPricesUnsafe` and `getEmaPricesUnsafe` are guarded by the `onlyWhitelistedReader` modifier, which checks `msg.sender` against the subscription manager and the reader whitelist. The contract address is never the manager and is never in the whitelist, so the inner call always reverts with `Unauthorized` whenever `whitelistEnabled` is `true`. The two "no older than" price-fetch functions are therefore permanently broken for any subscription that uses access control.

---

### Finding Description

`getPricesNoOlderThan` (line 535) passes the staleness check and then delegates to `getPricesUnsafe` via an external self-call:

```solidity
prices = this.getPricesUnsafe(subscriptionId, priceIds);   // line 554
```

`getPricesUnsafe` carries the `onlyWhitelistedReader(subscriptionId)` modifier:

```solidity
function getPricesUnsafe(
    uint256 subscriptionId,
    bytes32[] calldata priceIds
)
    external
    view
    override
    onlyWhitelistedReader(subscriptionId)          // line 521
    returns (PythStructs.Price[] memory prices)
```

The modifier first checks whether `msg.sender` is the subscription manager, then whether the whitelist is disabled (allowing any reader), and finally whether `msg.sender` is in the explicit whitelist:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // ... iterate whitelist ...
    if (!isWhitelisted) { revert SchedulerErrors.Unauthorized(); }
    _;
}
```

Because `this.getPricesUnsafe(...)` is an external call, the EVM sets `msg.sender = address(this)` for the inner frame. The contract address is never stored as the subscription manager and is never added to any reader whitelist, so the modifier always reaches `revert SchedulerErrors.Unauthorized()` when `whitelistEnabled == true`.

The identical pattern exists for `getEmaPricesNoOlderThan` → `this.getEmaPricesUnsafe(...)` at line 597.

---

### Impact Explanation

Any subscription that sets `whitelistEnabled = true` (the access-controlled mode) renders `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` permanently non-functional. Every call by a legitimately whitelisted reader reverts. The subscription manager cannot retrieve time-bounded prices either. The only working price-read path is the direct `getPricesUnsafe` / `getEmaPricesUnsafe`, which skips the staleness guarantee. This breaks a core read guarantee of the Scheduler contract for all access-controlled subscriptions.

---

### Likelihood Explanation

`whitelistEnabled` is a standard subscription parameter that any subscription creator can set. Any subscription that enables the whitelist immediately triggers the bug on every `getPricesNoOlderThan` / `getEmaPricesNoOlderThan` call. No special privilege or attack setup is required; the bug is deterministic and reproducible by any ordinary user.

---

### Recommendation

Replace the external `this.` self-calls with direct calls to the internal helper `_getPricesInternal`, which already contains the shared retrieval logic and does not carry the `onlyWhitelistedReader` modifier. The staleness check in the outer function is sufficient; the inner call does not need to re-run access control.

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

---

### Proof of Concept

1. Deploy `SchedulerUpgradeable`.
2. Call `createSubscription` with `whitelistEnabled = true` and a non-empty `readerWhitelist` containing address `A`.
3. From address `A` (a legitimately whitelisted reader), call `getPricesNoOlderThan(subscriptionId, priceIds, age)`.
4. The function passes the staleness check at line 551, then executes `this.getPricesUnsafe(subscriptionId, priceIds)` at line 554.
5. Inside `getPricesUnsafe`, `msg.sender == address(Scheduler contract)`. The modifier checks: contract ≠ manager → whitelist enabled → contract not in whitelist → `revert Unauthorized()`.
6. The transaction reverts despite `A` being a valid whitelisted reader.

The same steps with `getEmaPricesNoOlderThan` produce the identical revert via line 597. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L514-522)
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L557-565)
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
