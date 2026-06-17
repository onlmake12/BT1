### Title
`getPricesNoOlderThan` and `getEmaPricesNoOlderThan` Always Revert When Whitelist Is Enabled Due to Broken `this.` External Self-Call — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getPricesNoOlderThan` and `Scheduler.getEmaPricesNoOlderThan` delegate to `this.getPricesUnsafe(...)` and `this.getEmaPricesUnsafe(...)` via external self-calls. Because these are external calls, `msg.sender` inside the callee becomes `address(this)` (the Scheduler contract itself), not the original caller. Both `getPricesUnsafe` and `getEmaPricesUnsafe` are guarded by the `onlyWhitelistedReader` modifier, which checks `msg.sender` against the subscription's whitelist. When `whitelistEnabled = true`, the Scheduler contract address is never in the whitelist, so the modifier always reverts with `Unauthorized`. The two "no older than" functions are permanently broken for any subscription that uses a whitelist.

---

### Finding Description

`Scheduler.getPricesNoOlderThan` (line 554) and `Scheduler.getEmaPricesNoOlderThan` (line 597) both perform their staleness check and then delegate to the price-fetching logic via an external self-call:

```solidity
// Scheduler.sol line 554
prices = this.getPricesUnsafe(subscriptionId, priceIds);

// Scheduler.sol line 597
prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
```

`getPricesUnsafe` and `getEmaPricesUnsafe` are declared `external` and carry the `onlyWhitelistedReader(subscriptionId)` modifier:

```solidity
function getPricesUnsafe(
    uint256 subscriptionId,
    bytes32[] calldata priceIds
)
    external
    view
    override
    onlyWhitelistedReader(subscriptionId)   // ← checks msg.sender
    returns (PythStructs.Price[] memory prices)
```

The `onlyWhitelistedReader` modifier checks `msg.sender`:

```solidity
modifier onlyWhitelistedReader(uint256 subscriptionId) {
    if (_state.subscriptionManager[subscriptionId] == msg.sender) { _; return; }
    if (!_state.subscriptionParams[subscriptionId].whitelistEnabled) { _; return; }
    // ... check whitelist for msg.sender ...
    if (!isWhitelisted) { revert SchedulerErrors.Unauthorized(); }
    _;
}
```

When `getPricesNoOlderThan` calls `this.getPricesUnsafe(...)`, the EVM issues an external `CALL` to the contract itself. Inside `getPricesUnsafe`, `msg.sender` is now `address(this)` — the Scheduler contract — not the original caller. The Scheduler contract is never registered as a subscription manager or placed in any whitelist, so the modifier always reverts with `Unauthorized` whenever `whitelistEnabled = true`.

The internal helper `_getPricesInternal` already exists and is what `getPricesUnsafe` calls internally. The correct fix is to call `_getPricesInternal` directly instead of routing through `this.getPricesUnsafe(...)`.

---

### Impact Explanation

Any subscription created with `whitelistEnabled = true` renders `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` permanently non-functional. Whitelisted readers — the exact users these functions are designed to serve — can never successfully call them. The only working price-read path is `getPricesUnsafe` / `getEmaPricesUnsafe`, which provide no staleness guarantee. This breaks the safety contract of the Scheduler: consumers who rely on recency-checked prices are silently forced onto the unchecked path or receive a hard revert.

---

### Likelihood Explanation

The whitelist feature is a first-class, documented feature of the Scheduler (`whitelistEnabled`, `readerWhitelist` fields in `SubscriptionParams`). Any subscription owner who enables it and then calls `getPricesNoOlderThan` or `getEmaPricesNoOlderThan` will immediately hit the revert. No special attacker is needed — the bug is triggered by normal, intended usage. The entry point is fully unprivileged: any user can create a subscription and observe the failure.

---

### Recommendation

Replace the external self-calls in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with direct calls to the internal helper `_getPricesInternal`, then extract the `Price` array from the returned `PriceFeed` array inline. The `onlyWhitelistedReader` modifier on the outer function already enforces access control for the original caller; the redundant re-check via `this.` is both incorrect and unnecessary.

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

1. Deploy `SchedulerUpgradeable` with a valid Pyth address.
2. Call `createSubscription` with `whitelistEnabled = true` and `readerWhitelist = [alice]`, funding the minimum balance.
3. Call `updatePriceFeeds` to populate price data.
4. As `alice` (a whitelisted reader), call `getPricesNoOlderThan(subscriptionId, priceIds, 60)`.
5. The call reverts with `Unauthorized` because inside `getPricesUnsafe`, `msg.sender == address(Scheduler)`, which is not in the whitelist.
6. As `alice`, call `getPricesUnsafe(subscriptionId, priceIds)` directly — this succeeds, confirming `alice` is correctly whitelisted and the bug is isolated to the `this.` self-call path.

**Root cause lines:** [1](#0-0) [2](#0-1) 

**Modifier that rejects `address(this)` as caller:** [3](#0-2) 

**`getPricesUnsafe` and `getEmaPricesUnsafe` — the external callees with the blocking modifier:** [4](#0-3) [5](#0-4)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L550-555)
```text
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L593-598)
```text
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
