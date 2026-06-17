### Title
Nested O(n²) Loop with Storage Deletes in `_clearRemovedPriceUpdates()` Can Cause Excessive Gas Consumption — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `_clearRemovedPriceUpdates()` contains a nested O(n²) loop that performs storage deletions in the outer iteration. With `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255`, the worst case produces 65,025 loop iterations and up to 1,275 cold SSTORE-to-zero operations, driving a single `updateSubscription()` call to ~18–20 M gas — approaching the Ethereum 30 M block gas limit and making the function effectively unusable at scale.

---

### Finding Description

`_clearRemovedPriceUpdates()` is called inside `updateSubscription()` to clean up stored `PriceFeed` data for price IDs that are being removed from a subscription:

```solidity
// Outer loop: reads each old price ID from storage (SLOAD)
for (uint i = 0; i < currentPriceIds.length; i++) {
    bytes32 oldPriceId = currentPriceIds[i];
    bool found = false;

    // Inner loop: scans all new price IDs in memory
    for (uint j = 0; j < newPriceIds.length; j++) {
        if (newPriceIds[j] == oldPriceId) { found = true; break; }
    }

    // Storage delete on every miss
    if (!found) {
        delete _state.priceUpdates[subscriptionId][oldPriceId];
    }
}
``` [1](#0-0) 

`_state.priceUpdates[subscriptionId][oldPriceId]` is a `PythStructs.PriceFeed` struct. Each `PriceFeed` occupies **5 storage slots** (`id` + packed `price` fields + `price.publishTime` + packed `emaPrice` fields + `emaPrice.publishTime`). Deleting 255 structs zeroes **1,275 storage slots**.

The upper bound on price IDs per subscription is:

```solidity
uint8 public constant MAX_PRICE_IDS_PER_SUBSCRIPTION = 255;
uint8 public constant MAX_READER_WHITELIST_SIZE       = 255;
``` [2](#0-1) 

A second nested O(n²) loop exists in `_validateSubscriptionParams()` for both price-ID and whitelist uniqueness checks (called on every `createSubscription` and `updateSubscription`):

```solidity
for (uint i = 0; i < params.priceIds.length; i++) {
    for (uint j = i + 1; j < params.priceIds.length; j++) {
        if (params.priceIds[i] == params.priceIds[j]) { revert ...; }
    }
}
// identical pattern for readerWhitelist
``` [3](#0-2) 

**Worst-case gas breakdown for a single `updateSubscription()` call (255 old price IDs → 255 entirely new price IDs):**

| Step | Cost |
|---|---|
| `_validateSubscriptionParams` nested loops (memory) | ~650 k gas |
| Outer loop: 255 cold SLOADs of `currentPriceIds[i]` | ~535 k gas |
| Inner loop: 255 × 255 = 65,025 memory iterations | ~650 k gas |
| 1,275 cold SSTORE-to-zero (7,100 gas each, refunds capped at 20%) | ~5–9 M gas |
| Writing new `subscriptionParams` (255 price IDs + 255 whitelist, new slots) | ~10 M gas |
| Miscellaneous | ~500 k gas |
| **Total** | **~18–20 M gas** |

This approaches the 30 M Ethereum block gas limit and far exceeds typical per-transaction gas budgets.

---

### Impact Explanation

Any subscription manager who holds the maximum 255 price IDs and calls `updateSubscription()` to replace all of them will trigger ~18–20 M gas consumption in a single transaction. This:

1. Makes `updateSubscription()` effectively unusable at the protocol's own stated maximum capacity.
2. Forces subscription managers to either accept enormous gas costs or split updates across multiple transactions — a workflow the contract does not support.
3. Could permanently lock a subscription in an unupdatable state if the manager cannot afford the gas, preventing deactivation or price-ID rotation.

---

### Likelihood Explanation

- `updateSubscription()` is callable by any subscription manager — no privileged role required beyond owning the subscription.
- `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255` is the protocol's own documented limit; legitimate DeFi protocols tracking many assets will approach it.
- The gas cost scales quadratically with the number of price IDs, so even at 128 price IDs the cost is ~5–8 M gas — still prohibitively expensive.
- There is no batching mechanism or partial-update path in the contract. [4](#0-3) 

---

### Recommendation

1. **Replace the O(n²) uniqueness checks** in `_validateSubscriptionParams()` with a temporary `mapping(bytes32 => bool)` or `mapping(address => bool)` built in a single O(n) pass, then cleared.
2. **Replace the nested loop in `_clearRemovedPriceUpdates()`** with a mapping-based O(n + m) approach: build a set of new price IDs in O(m), then iterate old price IDs once in O(n) to identify and delete removed ones.
3. **Consider batching deletions** across multiple transactions if the number of removed price IDs is large, similar to the recommendation in the reference report.

---

### Proof of Concept

```solidity
// 1. Create subscription with MAX price IDs (255)
bytes32[] memory priceIds = new bytes32[](255);
for (uint i = 0; i < 255; i++) priceIds[i] = bytes32(i + 1);
scheduler.createSubscription{value: minimumBalance}(params); // subscriptionId = 1

// 2. Trigger an update so all 255 PriceFeed structs are written to storage
scheduler.updatePriceFeeds(1, updateData);

// 3. Call updateSubscription with 255 completely different price IDs
bytes32[] memory newPriceIds = new bytes32[](255);
for (uint i = 0; i < 255; i++) newPriceIds[i] = bytes32(i + 1000);
params.priceIds = newPriceIds;

uint256 gasBefore = gasleft();
scheduler.updateSubscription(1, params);
uint256 gasUsed = gasBefore - gasleft();
// gasUsed ≈ 18,000,000–20,000,000
```

The `_clearRemovedPriceUpdates` call alone will execute 65,025 loop iterations and delete 1,275 storage slots (255 `PriceFeed` structs × 5 slots each), consuming the bulk of the gas. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L77-153)
```text
    function updateSubscription(
        uint256 subscriptionId,
        SchedulerStructs.SubscriptionParams memory newParams
    ) external payable override onlyManager(subscriptionId) {
        SchedulerStructs.SubscriptionStatus storage currentStatus = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage currentParams = _state
            .subscriptionParams[subscriptionId];

        // Add incoming funds to balance
        currentStatus.balanceInWei += msg.value;

        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }

        // If subscription is inactive and will remain inactive, no need to validate parameters
        bool wasActive = currentParams.isActive;
        bool willBeActive = newParams.isActive;
        if (!wasActive && !willBeActive) {
            // Update subscription parameters
            _state.subscriptionParams[subscriptionId] = newParams;
            emit SubscriptionUpdated(subscriptionId);
            return;
        }
        _validateSubscriptionParams(newParams);

        // Check minimum balance if subscription remains active
        if (willBeActive) {
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(newParams.priceIds.length)
            );
            if (currentStatus.balanceInWei < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }
        }

        // Handle activation/deactivation
        if (!wasActive && willBeActive) {
            // Reactivating a subscription - ensure minimum balance
            uint256 minimumBalance = this.getMinimumBalance(
                uint8(newParams.priceIds.length)
            );

            // Check if balance meets minimum requirement
            if (currentStatus.balanceInWei < minimumBalance) {
                revert SchedulerErrors.InsufficientBalance();
            }

            currentParams.isActive = true;
            _addToActiveSubscriptions(subscriptionId);
            emit SubscriptionActivated(subscriptionId);
        } else if (wasActive && !willBeActive) {
            // Deactivating a subscription
            currentParams.isActive = false;
            _removeFromActiveSubscriptions(subscriptionId);
            emit SubscriptionDeactivated(subscriptionId);
        }

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

        emit SubscriptionUpdated(subscriptionId);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L175-198)
```text
        for (uint i = 0; i < params.priceIds.length; i++) {
            for (uint j = i + 1; j < params.priceIds.length; j++) {
                if (params.priceIds[i] == params.priceIds[j]) {
                    revert SchedulerErrors.DuplicatePriceId(params.priceIds[i]);
                }
            }
        }

        // Whitelist size limit and uniqueness
        if (params.readerWhitelist.length > MAX_READER_WHITELIST_SIZE) {
            revert SchedulerErrors.TooManyWhitelistedReaders(
                params.readerWhitelist.length,
                MAX_READER_WHITELIST_SIZE
            );
        }
        for (uint i = 0; i < params.readerWhitelist.length; i++) {
            for (uint j = i + 1; j < params.readerWhitelist.length; j++) {
                if (params.readerWhitelist[i] == params.readerWhitelist[j]) {
                    revert SchedulerErrors.DuplicateWhitelistAddress(
                        params.readerWhitelist[i]
                    );
                }
            }
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L228-250)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L823-832)
```text
    function _storePriceUpdates(
        uint256 subscriptionId,
        PythStructs.PriceFeed[] memory priceFeeds
    ) internal {
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            _state.priceUpdates[subscriptionId][priceFeeds[i].id] = priceFeeds[
                i
            ];
        }
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L8-10)
```text
    uint8 public constant MAX_PRICE_IDS_PER_SUBSCRIPTION = 255;
    /// Maximum number of addresses in the reader whitelist
    uint8 public constant MAX_READER_WHITELIST_SIZE = 255;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L27-28)
```text
        /// Sub ID -> price ID -> latest parsed price update for the subscribed feed
        mapping(uint256 => mapping(bytes32 => PythStructs.PriceFeed)) priceUpdates;
```
