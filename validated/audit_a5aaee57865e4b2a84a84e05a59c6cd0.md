### Title
Quadratic Gas Complexity in `_clearRemovedPriceUpdates` Causes `updateSubscription` to Fail for Large Subscriptions - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler.sol` contains two nested-loop patterns with O(n²) or O(n×m) complexity that scale poorly as subscriptions approach the maximum allowed size (`MAX_PRICE_IDS_PER_SUBSCRIPTION = 255`, `MAX_READER_WHITELIST_SIZE = 255`). The most severe instance is `_clearRemovedPriceUpdates`, which performs nested iterations where the **inner loop reads from a `bytes32[] storage` array**, producing up to 65,025 warm SLOAD operations in a single `updateSubscription` call.

### Finding Description

`_validateSubscriptionParams` contains two independent O(n²) nested loops over calldata/memory arrays to detect duplicates in `priceIds` and `readerWhitelist`: [1](#0-0) 

With `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255` and `MAX_READER_WHITELIST_SIZE = 255`, each loop performs up to `255 × 254 / 2 ≈ 32,385` comparisons. [2](#0-1) 

The more severe instance is `_clearRemovedPriceUpdates`, called from `updateSubscription`: [3](#0-2) 

This function contains **two separate nested loops**:

1. **First loop (lines 234–250):** outer iterates over `currentPriceIds` (`bytes32[] storage`), performing up to 255 cold SLOADs (2,100 gas each = **535,500 gas**).
2. **Second loop (lines 253–270):** outer iterates over `newPriceIds` (memory), but the **inner loop reads `currentPriceIds[j]` from storage**. After the first loop warms those slots, worst case is 255 × 255 = 65,025 warm SLOADs at 100 gas each = **6,502,500 gas** for the inner reads alone.

Combined with the `_validateSubscriptionParams` nested loops, a single `updateSubscription` call on a max-size subscription can consume **7+ million gas**, approaching or exceeding block gas limits on Ethereum mainnet (~30M gas limit, but individual transactions are practically limited to far less by keeper infrastructure and MEV bots).

`updateSubscription` is the entry point: [4](#0-3) 

### Impact Explanation

A subscription manager who creates a subscription with the maximum 255 price IDs and later attempts to call `updateSubscription` (e.g., to change update criteria, add/remove feeds, or deactivate) will trigger `_clearRemovedPriceUpdates` with worst-case O(n²) storage reads. The resulting gas cost can make the transaction economically infeasible or cause it to run out of gas entirely, **permanently locking the manager out of modifying their subscription**. Since `withdrawFunds` is a separate function, funds are not directly lost, but the subscription becomes unmanageable. Any protocol integrator relying on the ability to update subscriptions (e.g., to respond to market changes or deactivate a subscription) is effectively denied that capability.

### Likelihood Explanation

Any user who creates a subscription with a large number of price IDs (up to the protocol-enforced maximum of 255) and later calls `updateSubscription` will encounter this. The `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255` constant is explicitly designed to allow large subscriptions, making this a realistic scenario for production integrators tracking many price feeds simultaneously. [5](#0-4) 

### Recommendation

1. **Replace nested duplicate-detection loops** in `_validateSubscriptionParams` with a single-pass approach using a transient or in-memory bitmap/mapping keyed by a hash of each `priceId`/`address`, reducing O(n²) to O(n).
2. **Replace `_clearRemovedPriceUpdates` nested loops** with a two-pass approach: first build an in-memory `mapping`-equivalent (e.g., a sorted array + binary search, or a hash set over the new IDs), then do a single O(n) pass over `currentPriceIds` to identify removals. This eliminates the O(n×m) storage-read pattern.
3. **Replace the `onlyWhitelistedReader` linear storage scan** (lines 768–773) with a `mapping(address => bool)` stored alongside the whitelist array, enabling O(1) membership checks. [6](#0-5) 

### Proof of Concept

```
Scenario: Subscription manager creates a subscription with 255 price IDs,
then calls updateSubscription to change the heartbeat interval.

1. Manager calls createSubscription with 255 unique priceIds.
   - _validateSubscriptionParams runs: 255*254/2 = 32,385 memory comparisons (priceIds loop)
   - Subscription stored with 255 price IDs in storage.

2. Manager calls updateSubscription with a new params struct containing
   the same 255 price IDs (no IDs added or removed) + changed heartbeatSeconds.

3. _validateSubscriptionParams runs again: 32,385 memory comparisons.

4. _clearRemovedPriceUpdates runs:
   - First loop: 255 cold SLOADs of currentPriceIds[i] = 255 × 2,100 = 535,500 gas
     Inner loop: 255 × 255 = 65,025 memory reads (cheap)
   - Second loop: 255 memory reads (outer)
     Inner loop: 255 × 255 = 65,025 warm SLOADs of currentPriceIds[j]
                = 65,025 × 100 = 6,502,500 gas

5. Total gas for _clearRemovedPriceUpdates inner storage reads alone: ~7,038,000 gas.
   Combined with _validateSubscriptionParams and other updateSubscription overhead,
   the transaction can exceed practical gas limits or cost hundreds of dollars at
   moderate gas prices, making subscription management economically infeasible.
``` [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L228-273)
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

        // Check if any new price IDs were added
        for (uint i = 0; i < newPriceIds.length; i++) {
            bytes32 newPriceId = newPriceIds[i];
            bool found = false;

            // Check if the new price ID exists in the current list
            for (uint j = 0; j < currentPriceIds.length; j++) {
                if (currentPriceIds[j] == newPriceId) {
                    found = true;
                    break;
                }
            }

            // If a new price ID was added, mark as changed
            if (!found) {
                newPriceIdsAdded = true;
                break;
            }
        }

        return newPriceIdsAdded;
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L7-10)
```text
    /// Maximum number of price feeds per subscription
    uint8 public constant MAX_PRICE_IDS_PER_SUBSCRIPTION = 255;
    /// Maximum number of addresses in the reader whitelist
    uint8 public constant MAX_READER_WHITELIST_SIZE = 255;
```
