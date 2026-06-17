### Title
Validation Bypass in `updateSubscription` Allows Storing Invalid Subscription Parameters on Inactive Subscriptions — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

In `Scheduler.sol`, `updateSubscription` contains an early-return path that skips `_validateSubscriptionParams` entirely when a subscription is currently inactive and will remain inactive (`!wasActive && !willBeActive`). This is the direct structural analog of the Lava Network finding: a validation function exists and is called in the primary code path, but a secondary code path bypasses it, allowing invalid parameters to be committed to storage. The most concrete downstream consequence is that a manager can set `isPermanent = true` on an inactive subscription through this bypass, permanently locking the subscription in an inactive state and making deposited funds irrecoverable.

### Finding Description

In `updateSubscription`, the guard at lines 97–102 reads:

```solidity
if (!wasActive && !willBeActive) {
    // Update subscription parameters
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;
}
_validateSubscriptionParams(newParams);
```

When both `wasActive` and `willBeActive` are `false`, the function writes `newParams` directly to `_state.subscriptionParams[subscriptionId]` and returns, completely skipping `_validateSubscriptionParams`. This means any caller who is the subscription manager can supply `newParams` containing:

- `priceIds.length == 0` (violates `EmptyPriceIds`)
- `priceIds.length > MAX_PRICE_IDS_PER_SUBSCRIPTION` (violates `TooManyPriceIds`)
- Duplicate entries in `priceIds` (violates `DuplicatePriceId`)
- `readerWhitelist.length > MAX_READER_WHITELIST_SIZE` (violates `TooManyWhitelistedReaders`)
- `updateOnHeartbeat = true` with `heartbeatSeconds = 0` (violates `InvalidUpdateCriteria`)
- `updateOnDeviation = true` with `deviationThresholdBps = 0` (violates `InvalidUpdateCriteria`)
- Both `updateOnHeartbeat = false` and `updateOnDeviation = false` (violates `InvalidUpdateCriteria`)
- `isPermanent = true` (not checked by `_validateSubscriptionParams`, but has irreversible state consequences)

The most impactful exploit path uses `isPermanent = true`. Once stored through the bypass, the `isPermanent` check at line 90 fires on every subsequent `updateSubscription` call:

```solidity
if (currentParams.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
```

And `withdrawFunds` at line 640 also blocks:

```solidity
if (params.isPermanent) {
    revert SchedulerErrors.CannotUpdatePermanentSubscription();
}
```

The subscription is permanently inactive and all deposited ETH is irrecoverable.

### Impact Explanation

A subscription manager who deactivates their subscription and then calls `updateSubscription` with `isActive = false, isPermanent = true` (or any other invalid params) will have those params committed to storage without any validation. If `isPermanent = true` is stored, the manager permanently loses the ability to:

- Reactivate the subscription (blocked by `CannotUpdatePermanentSubscription`)
- Update the subscription (same block)
- Withdraw deposited ETH (same block via `withdrawFunds`)

The deposited ETH balance (`status.balanceInWei`) is permanently locked in the contract with no recovery path. The subscription is also permanently excluded from `updatePriceFeeds` since it is inactive, making the locked funds entirely unproductive.

### Likelihood Explanation

The trigger requires the subscription manager to call `updateSubscription` on their own inactive subscription with `isPermanent = true`. This can occur accidentally (e.g., a manager copies params from a permanent subscription template and applies them to an inactive one) or through a buggy integration contract acting as manager. The `onlyManager` modifier restricts the call to the manager, so external attackers cannot trigger this against other users' subscriptions. Likelihood is low-to-medium for accidental self-harm, and not applicable for third-party exploitation.

### Recommendation

Move the `_validateSubscriptionParams` call before the early-return branch, or apply it unconditionally regardless of the `wasActive`/`willBeActive` state. At minimum, the `isPermanent` flag transition should be validated separately to ensure it cannot be set on an inactive subscription:

```solidity
// Always validate before storing
_validateSubscriptionParams(newParams);

if (!wasActive && !willBeActive) {
    _state.subscriptionParams[subscriptionId] = newParams;
    emit SubscriptionUpdated(subscriptionId);
    return;
}
```

### Proof of Concept

1. Alice calls `createSubscription` with valid params and deposits ETH. Subscription ID = 1, `isActive = true`, `isPermanent = false`.
2. Alice calls `updateSubscription(1, {isActive: false, isPermanent: false, ...valid})`. Subscription is deactivated. `wasActive = true`, `willBeActive = false` → validation runs, params stored normally.
3. Alice calls `updateSubscription(1, {isActive: false, isPermanent: true, heartbeatSeconds: 0, updateOnHeartbeat: true, priceIds: []})`. `wasActive = false`, `willBeActive = false` → early return taken, `_validateSubscriptionParams` is **never called**, invalid params including `isPermanent = true` are written to `_state.subscriptionParams[1]`.
4. Alice calls `updateSubscription(1, {isActive: true, ...valid})` to reactivate. Reverts: `CannotUpdatePermanentSubscription`.
5. Alice calls `withdrawFunds(1, amount)`. Reverts: `CannotUpdatePermanentSubscription`.
6. Alice's deposited ETH is permanently locked. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L89-92)
```text
        // Updates to permanent subscriptions are not allowed
        if (currentParams.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L94-102)
```text
        // If subscription is inactive and will remain inactive, no need to validate parameters
        bool wasActive = currentParams.isActive;
        bool willBeActive = newParams.isActive;
        if (!wasActive && !willBeActive) {
            // Update subscription parameters
            _state.subscriptionParams[subscriptionId] = newParams;
            emit SubscriptionUpdated(subscriptionId);
            return;
        }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L157-219)
```text
    function _validateSubscriptionParams(
        SchedulerStructs.SubscriptionParams memory params
    ) internal pure {
        // No zero‐feed subscriptions
        if (params.priceIds.length == 0) {
            revert SchedulerErrors.EmptyPriceIds();
        }

        // Price ID limits and uniqueness
        if (
            params.priceIds.length >
            SchedulerConstants.MAX_PRICE_IDS_PER_SUBSCRIPTION
        ) {
            revert SchedulerErrors.TooManyPriceIds(
                params.priceIds.length,
                SchedulerConstants.MAX_PRICE_IDS_PER_SUBSCRIPTION
            );
        }
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

        // Validate update criteria
        if (
            !params.updateCriteria.updateOnHeartbeat &&
            !params.updateCriteria.updateOnDeviation
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
        if (
            params.updateCriteria.updateOnHeartbeat &&
            params.updateCriteria.heartbeatSeconds == 0
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
        if (
            params.updateCriteria.updateOnDeviation &&
            params.updateCriteria.deviationThresholdBps == 0
        ) {
            revert SchedulerErrors.InvalidUpdateCriteria();
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L639-642)
```text
        // Prevent withdrawals from permanent subscriptions
        if (params.isPermanent) {
            revert SchedulerErrors.CannotUpdatePermanentSubscription();
        }
```
