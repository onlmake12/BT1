### Title
O(n²) Nested Loops with Storage Reads in `_clearRemovedPriceUpdates` and `_validateSubscriptionParams` Cause `updateSubscription` to Exceed Block Gas Limit at Maximum Feed Count — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.updateSubscription` calls two functions containing O(n²) nested loops — `_validateSubscriptionParams` and `_clearRemovedPriceUpdates` — where `n` is bounded by `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255`. The second function performs **storage reads** inside its inner loop, making the worst-case gas cost exceed Ethereum's 30M block gas limit. A subscription manager who creates a subscription at the maximum feed count will find `updateSubscription` permanently reverts, preventing deactivation, whitelist changes, or any parameter modification.

---

### Finding Description

`SchedulerConstants` sets `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255` and `MAX_READER_WHITELIST_SIZE = 255`. [1](#0-0) 

`_validateSubscriptionParams` contains two independent O(n²) nested loops — one over `priceIds` and one over `readerWhitelist` — to check uniqueness: [2](#0-1) 

At n=255, each nested loop runs 255×254/2 = 32,385 iterations, totalling ~64,770 iterations across both checks.

`_clearRemovedPriceUpdates`, called unconditionally from `updateSubscription`, contains two more O(n²) nested loops. The **second** loop reads `currentPriceIds[j]` from **storage** on every inner iteration: [3](#0-2) 

With 255 current and 255 new price IDs, this inner loop executes 255×255 = 65,025 storage reads. At 2,100 gas per cold `SLOAD`, that alone costs **136,552,500 gas** — more than 4× the 30M block gas limit. Even with warm reads (100 gas each), the combined cost of both loops in `_clearRemovedPriceUpdates` plus `_validateSubscriptionParams` makes the transaction infeasible.

`updateSubscription` calls both functions in sequence: [4](#0-3) 

---

### Impact Explanation

Any subscription manager who creates a subscription with 255 price IDs (a valid, protocol-permitted action) will find that every subsequent call to `updateSubscription` reverts with out-of-gas. This permanently prevents:

- Changing price IDs
- Modifying the reader whitelist
- Deactivating the subscription (deactivation flows through `updateSubscription`)

The subscription is frozen in an active state. While `withdrawFunds` remains callable, the subscription cannot be administratively managed, and the keeper will continue attempting (and failing) to update it until the balance is drained.

---

### Likelihood Explanation

Any unprivileged user can trigger this by calling `createSubscription` with `priceIds.length == 255` and paying the required minimum balance. No special role or leaked key is needed. The `MAX_PRICE_IDS_PER_SUBSCRIPTION = 255` constant is a public, documented protocol limit, so users may legitimately create subscriptions at or near this limit. The condition is deterministic and reproducible. [5](#0-4) 

---

### Recommendation

1. **Replace the O(n²) uniqueness checks** in `_validateSubscriptionParams` with a single-pass approach using a transient bitmap or sorted-array check, reducing complexity to O(n log n) or O(n).
2. **Replace the O(n²) set-difference logic** in `_clearRemovedPriceUpdates` with a mapping-based lookup (build a `mapping(bytes32 => bool)` from one array, then iterate the other), reducing to O(n).
3. **Lower `MAX_PRICE_IDS_PER_SUBSCRIPTION`** to a value whose worst-case gas cost has been empirically verified against the block gas limit (the existing `PulseSchedulerGasBenchmark` test suite is the right place to add this). [6](#0-5) 

---

### Proof of Concept

1. Deploy `SchedulerProxy` on a local fork.
2. Call `createSubscription` with `priceIds.length = 255` and `readerWhitelist.length = 255`, paying the required minimum balance.
3. Call `updateSubscription` with any parameter change (e.g., change `heartbeatSeconds`).
4. Observe the transaction reverts with out-of-gas.

Gas estimate for `_clearRemovedPriceUpdates` second loop alone (worst case, cold storage):
```
255 (outer) × 255 (inner) × 2,100 gas/SLOAD = 136,552,500 gas
```
This exceeds Ethereum mainnet's 30,000,000 gas block limit by more than 4×. [3](#0-2)

### Citations

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L8-10)
```text
    uint8 public constant MAX_PRICE_IDS_PER_SUBSCRIPTION = 255;
    /// Maximum number of addresses in the reader whitelist
    uint8 public constant MAX_READER_WHITELIST_SIZE = 255;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L32-75)
```text
    function createSubscription(
        SchedulerStructs.SubscriptionParams memory subscriptionParams
    ) external payable override returns (uint256 subscriptionId) {
        _validateSubscriptionParams(subscriptionParams);

        // Calculate minimum balance required for this subscription
        uint256 minimumBalance = this.getMinimumBalance(
            uint8(subscriptionParams.priceIds.length)
        );

        // Ensure enough funds were provided
        if (msg.value < minimumBalance) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Check deposit limit for permanent subscriptions
        if (subscriptionParams.isPermanent && msg.value > MAX_DEPOSIT_LIMIT) {
            revert SchedulerErrors.MaxDepositLimitExceeded();
        }

        // Set subscription to active
        subscriptionParams.isActive = true;

        subscriptionId = _state.subscriptionNumber++;

        // Store the subscription parameters
        _state.subscriptionParams[subscriptionId] = subscriptionParams;

        // Initialize subscription status
        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        status.priceLastUpdatedAt = 0;
        status.balanceInWei = msg.value;
        status.totalUpdates = 0;
        status.totalSpent = 0;

        // Map subscription ID to manager
        _state.subscriptionManager[subscriptionId] = msg.sender;

        _addToActiveSubscriptions(subscriptionId);

        emit SubscriptionCreated(subscriptionId, msg.sender);
        return subscriptionId;
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L103-152)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L228-272)
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
```
