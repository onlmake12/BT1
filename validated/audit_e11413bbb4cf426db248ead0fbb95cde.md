### Title
Swap-and-Pop in `_removeFromActiveSubscriptions` Causes Keeper Pagination to Skip Subscriptions — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler._removeFromActiveSubscriptions` uses a swap-and-pop pattern on the `activeSubscriptionIds` array. `getActiveSubscriptions(startIndex, maxResults)` is a positional paginator over that same array. When a subscription manager deactivates a subscription whose array slot falls within an already-fetched page, the last element of the array is moved into that slot. Any keeper that is mid-pagination will never visit the moved element, silently skipping it for the entire polling cycle.

### Finding Description

`_removeFromActiveSubscriptions` removes an element by swapping it with the last element and calling `.pop()`:

```solidity
// Scheduler.sol lines 806-815
if (index < _state.activeSubscriptionIds.length - 1) {
    uint256 lastId = _state.activeSubscriptionIds[
        _state.activeSubscriptionIds.length - 1
    ];
    _state.activeSubscriptionIds[index] = lastId;
    _state.activeSubscriptionIndex[lastId] = index + 1;
}
_state.activeSubscriptionIds.pop();
_state.activeSubscriptionIndex[subscriptionId] = 0;
``` [1](#0-0) 

`getActiveSubscriptions` is a pure positional paginator — it reads `activeSubscriptionIds[startIndex + i]` with no snapshot or cursor:

```solidity
// Scheduler.sol lines 684-729
function getActiveSubscriptions(uint256 startIndex, uint256 maxResults) ...
    for (uint256 i = 0; i < resultCount; i++) {
        uint256 subscriptionId = _state.activeSubscriptionIds[startIndex + i];
        ...
    }
``` [2](#0-1) 

The interface NatSpec acknowledges order instability but does not warn about completeness loss during multi-page enumeration:

```
/// @dev Note that the order of subscription IDs returned may not be sequential
///      and can change when subscriptions are deactivated or reactivated.
``` [3](#0-2) 

**Concrete scenario:**

| Step | Array state (indices 0–10) | Keeper action |
|------|---------------------------|---------------|
| 1 | `[A,B,C,D,E,F,G,H,I,J,K]` | Page 1: `getActiveSubscriptions(0,5)` → `[A,B,C,D,E]` |
| 2 | Manager deactivates `D` (index 3) → `K` (index 10) swapped to index 3 | — |
| 3 | `[A,B,C,K,E,F,G,H,I,J]` | Page 2: `getActiveSubscriptions(5,5)` → `[F,G,H,I,J]` |

`K` was at index 10 (not yet visited), is now at index 3 (already past), and is **never returned** in this polling cycle.

The `SubscriptionState` in the Argus keeper service is populated entirely from `getActiveSubscriptions` calls: [4](#0-3) 

### Impact Explanation

A subscription whose `subscriptionId` is silently dropped from the keeper's working set will not receive `updatePriceFeeds` calls for the entire polling interval. For heartbeat-based subscriptions this means the on-chain price can be stale for up to `2 × heartbeatSeconds`. For deviation-based subscriptions a large price move goes undetected for one full cycle. DeFi protocols reading those prices via `getPricesNoOlderThan` will revert or consume a stale price depending on their staleness tolerance.

### Likelihood Explanation

Any subscription manager — an unprivileged role — can call `updateSubscription` to deactivate their subscription at any time. No special access is required. Keepers are expected to paginate `getActiveSubscriptions` continuously. The race condition is therefore a routine operational state, not a contrived edge case. A malicious actor who owns any subscription can time a deactivation to reliably push a targeted high-value subscription (e.g., ETH/USD) out of the keeper's current page window.

### Recommendation

Replace the positional `startIndex` pagination with a cursor based on the stable, monotonically increasing `subscriptionId` (the mapping key, not the array position). Alternatively, emit a `SubscriptionDeactivated` event and have keepers maintain their own set rather than re-paginating the mutable array. A simpler on-chain fix is to return the full `activeSubscriptionIds` array in a single call (bounded by a gas cap) so no multi-page state is held across blocks.

### Proof of Concept

1. Deploy `Scheduler` with 11 subscriptions (IDs 1–11, array indices 0–10).
2. Keeper calls `getActiveSubscriptions(0, 5)` → receives IDs at indices 0–4 (subscriptions 1–5).
3. Manager of subscription 4 (index 3) calls `updateSubscription` with `isActive = false`. `_removeFromActiveSubscriptions` swaps subscription 11 (index 10) into index 3 and pops.
4. Keeper calls `getActiveSubscriptions(5, 5)` → receives IDs at indices 5–9 (subscriptions 6–10). Subscription 11 is now at index 3 and is never returned.
5. Subscription 11's price feeds are not updated for the entire polling cycle. Any DeFi consumer of subscription 11 reads a stale price. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L684-729)
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
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L797-818)
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
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L124-132)
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
```

**File:** apps/argus/src/services/subscription_service.rs (L51-65)
```rust
    async fn refresh_subscriptions(&self) -> Result<()> {
        match self.contract.get_active_subscriptions().await {
            Ok(subscriptions) => {
                tracing::info!(
                    service_name = self.name,
                    subscription_count = subscriptions.len(),
                    "Retrieved active subscriptions"
                );

                self.subscription_state.update_subscriptions(subscriptions);

                let feed_ids = self.subscription_state.get_feed_ids();
                self.pyth_price_state.update_feed_ids(feed_ids.clone());
                self.chain_price_state.update_feed_ids(feed_ids);

```

**File:** target_chains/ethereum/contracts/contracts/pulse/SchedulerState.sol (L31-37)
```text
        /// Array of active subscription IDs.
        /// Gas optimization to avoid scanning through all subscriptions when querying for all active ones.
        uint256[] activeSubscriptionIds;
        /// Sub ID -> index in activeSubscriptionIds array + 1 (0 means not in array).
        /// This lets us avoid a linear scan of `activeSubscriptionIds` when deactivating a subscription.
        mapping(uint256 => uint256) activeSubscriptionIndex;
    }
```
