### Title
Unstable `activeSubscriptionIds` Ordering via Swap-and-Pop Causes Keeper to Skip Active Subscriptions During Pagination — (`target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol` maintains `activeSubscriptionIds` using a swap-and-pop removal pattern. The public `getActiveSubscriptions` function exposes this array via positional pagination (`startIndex`, `maxResults`). When any subscription manager deactivates their subscription between two paginated keeper calls, the last element in the array is moved to fill the gap, causing a different active subscription to be silently skipped by the keeper in that scan cycle.

---

### Finding Description

`_removeFromActiveSubscriptions` implements a classic swap-and-pop: [1](#0-0) 

When element at index `i` is removed, the last element is moved to index `i`, and the array is shortened. The `getActiveSubscriptions` function returns subscriptions by positional slice: [2](#0-1) 

The interface NatSpec acknowledges the instability but does not address the pagination race: [3](#0-2) 

**Concrete skip scenario:**

| Step | Array state | Keeper action |
|---|---|---|
| Initial | `[A, B, C, D, E]` (len=5) | — |
| Keeper call 1 | `[A, B, C, D, E]` | Fetches indices 0–2 → `{A, B, C}` |
| Manager deactivates C | `[A, B, E, D]` (len=4) | E swapped to index 2 |
| Keeper call 2 | `[A, B, E, D]` | Fetches indices 3–4 → `{D}` only |
| **Result** | E was at index 4, now at index 2 (already-fetched range) | **E is never processed** |

Subscription E is a fully funded, active subscription whose price feeds are not updated in this keeper cycle.

---

### Impact Explanation

The Argus keeper service uses `getActiveSubscriptions` with pagination to discover which subscriptions need price updates and calls `updatePriceFeeds` for each: [4](#0-3) 

When subscription E is skipped, its stored prices go stale. Consumers of subscription E (DeFi protocols using `getPricesUnsafe` or `getPricesNoOlderThan`) receive outdated prices, which can cause incorrect liquidations, mispriced collateral, or missed arbitrage protection. The subscription's balance is also not charged for the missed update, meaning the subscription manager of E effectively receives fewer updates than paid for.

---

### Likelihood Explanation

Any subscription manager can deactivate their own subscription at any time — this is a normal, permissionless operation. No special privilege is required. The deactivation of subscription C is a legitimate action that has the side effect of displacing subscription E in the array. A malicious actor who wants to grief a specific subscription (E) only needs to identify which subscription occupies the last slot in `activeSubscriptionIds` and time their own deactivation to coincide with a keeper's pagination boundary. The keeper's pagination pattern is deterministic and observable on-chain.

---

### Recommendation

1. **Use a stable removal strategy**: Instead of swap-and-pop, use a linked-list or a tombstone/bitmap approach that does not reorder elements.
2. **Alternatively, document and fix the keeper**: The Argus keeper should re-fetch `totalCount` after each page and restart the scan if it changes, or use subscription IDs (not positional indices) as cursors.
3. **Emit an event on removal**: Emit an event when `_removeFromActiveSubscriptions` is called so off-chain services can detect list mutations and invalidate their pagination state.

---

### Proof of Concept

1. Deploy `Scheduler` with 5 active subscriptions: IDs `[10, 20, 30, 40, 50]` at indices `[0, 1, 2, 3, 4]`.
2. Keeper calls `getActiveSubscriptions(0, 3)` → receives `[10, 20, 30]`.
3. Manager of subscription `30` (index 2) calls `updateSubscription(30, {isActive: false})`.
   - `_removeFromActiveSubscriptions(30)`: subscription `50` (last, index 4) is moved to index 2.
   - Array is now `[10, 20, 50, 40]` (len=4).
4. Keeper calls `getActiveSubscriptions(3, 3)` → receives `[40]` only (index 3).
5. Subscription `50` (now at index 2) is never returned in either call.
6. `updatePriceFeeds` is never called for subscription `50` in this cycle; its prices go stale. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L684-730)
```text
    function getActiveSubscriptions(
        uint256 startIndex,
        uint256 maxResults
    )
        external
        view
        override
        returns (
            uint256[] memory subscriptionIds,
            SchedulerStructs.SubscriptionParams[] memory subscriptionParams,
            uint256 totalCount
        )
    {
        totalCount = _state.activeSubscriptionIds.length;

        // If startIndex is beyond the total count, return empty arrays
        if (startIndex >= totalCount) {
            return (
                new uint256[](0),
                new SchedulerStructs.SubscriptionParams[](0),
                totalCount
            );
        }

        // Calculate how many results to return (bounded by maxResults and remaining items)
        uint256 resultCount = totalCount - startIndex;
        if (resultCount > maxResults) {
            resultCount = maxResults;
        }

        // Create arrays for subscription IDs and parameters
        subscriptionIds = new uint256[](resultCount);
        subscriptionParams = new SchedulerStructs.SubscriptionParams[](
            resultCount
        );

        // Populate the arrays with the requested page of active subscriptions
        for (uint256 i = 0; i < resultCount; i++) {
            uint256 subscriptionId = _state.activeSubscriptionIds[
                startIndex + i
            ];
            subscriptionIds[i] = subscriptionId;
            subscriptionParams[i] = _state.subscriptionParams[subscriptionId];
        }

        return (subscriptionIds, subscriptionParams, totalCount);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L797-817)
```text
    function _removeFromActiveSubscriptions(uint256 subscriptionId) internal {
        uint256 index = _state.activeSubscriptionIndex[subscriptionId];

        // Only remove if it's in the list
        if (index > 0) {
            // Adjust index to be 0-based instead of 1-based
            index = index - 1;

            // If it's not the last element, move the last element to its position
            if (index < _state.activeSubscriptionIds.length - 1) {
                uint256 lastId = _state.activeSubscriptionIds[
                    _state.activeSubscriptionIds.length - 1
                ];
                _state.activeSubscriptionIds[index] = lastId;
                _state.activeSubscriptionIndex[lastId] = index + 1; // 1-based index
            }

            // Remove the last element
            _state.activeSubscriptionIds.pop();
            _state.activeSubscriptionIndex[subscriptionId] = 0;
        }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L124-143)
```text
    /// @notice Gets all active subscriptions with their parameters, paginated.
    /// @dev This function has no access control to allow keepers to discover active subscriptions.
    /// @dev Note that the order of subscription IDs returned may not be sequential and can change
    ///      when subscriptions are deactivated or reactivated.
    /// @param startIndex The starting index within the list of active subscriptions (NOT the subscription ID).
    /// @param maxResults The maximum number of results to return starting from startIndex.
    /// @return subscriptionIds Array of active subscription IDs
    /// @return subscriptionParams Array of subscription parameters for each active subscription
    /// @return totalCount Total number of active subscriptions
    function getActiveSubscriptions(
        uint256 startIndex,
        uint256 maxResults
    )
        external
        view
        returns (
            uint256[] memory subscriptionIds,
            SchedulerStructs.SubscriptionParams[] memory subscriptionParams,
            uint256 totalCount
        );
```

**File:** apps/argus/src/services/subscription_service.rs (L1-1)
```rust
//! Subscription Service
```
