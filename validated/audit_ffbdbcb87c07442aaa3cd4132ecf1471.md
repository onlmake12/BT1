### Title
Incomplete Balance Pre-Check in `Scheduler.updatePriceFeeds` Omits Keeper Fee, Enabling Keeper Gas Drain and Subscription DoS - (File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol)

---

### Summary

`Scheduler.updatePriceFeeds` validates only the Pyth protocol fee (`pythFee`) before performing all expensive operations. The keeper fee is checked only at the very end, inside `_processFeesAndPayKeeper`. Any subscription whose balance satisfies `pythFee ≤ balance < pythFee + keeperFee` will pass the initial guard, trigger a full external Pyth call, price-feed validation, and storage writes, and then revert — wasting the keeper's gas and leaving the subscription permanently unupdatable until the manager tops up funds.

---

### Finding Description

In `updatePriceFeeds`, the only upfront balance guard is:

```solidity
// Line 295
if (status.balanceInWei < pythFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [1](#0-0) 

After this check passes, the function immediately deducts `pythFee` and performs expensive work:

1. `status.balanceInWei -= pythFee` (line 305)
2. External call: `pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(...)` (line 311)
3. `_validateShouldUpdatePrices(...)` — reads storage, iterates feeds
4. `_storePriceUpdates(...)` — writes storage for every price feed [2](#0-1) 

Only **after** all of that does `_processFeesAndPayKeeper` check whether the remaining balance covers the keeper fee:

```solidity
uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) * numPriceIds;
uint256 totalKeeperFee = gasCost + keeperSpecificFee;

if (status.balanceInWei < totalKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
``` [3](#0-2) 

Because the revert unwinds all state changes (including the `pythFee` deduction), the subscription balance is restored to its pre-call value. The subscription therefore remains in the same stuck state on every subsequent call: every keeper attempt passes the initial guard, burns gas through the expensive path, and reverts.

The total cost of an update is `pythFee + keeperFee`, but the pre-check only validates `pythFee`. This is the direct analog of the reference report's omission of the transaction `Value` from the `Cost` function.

---

### Impact Explanation

- **Keeper gas drain**: Every call to `updatePriceFeeds` on a stuck subscription executes the full external Pyth call and storage writes before reverting. The keeper bears this gas cost with no compensation.
- **Subscription permanently stuck**: The subscription remains active in `getActiveSubscriptions` and appears serviceable, but no update can ever succeed until the manager adds funds. Price data served to readers becomes stale.
- **Amplified DoS**: An attacker creates subscriptions with a very short heartbeat (e.g., 1 second), funds them to `minimumBalance`, and lets normal updates drain the balance into the stuck range. From that point, every keeper polling the active subscription list wastes gas on guaranteed-reverting calls. The attacker's cost is bounded by `minimumBalance`; the keeper's repeated gas loss is unbounded.

---

### Likelihood Explanation

- **Natural occurrence**: Any subscription that is not actively topped up will eventually reach a balance in the range `[pythFee, pythFee + keeperFee)` as updates drain it. This is a normal operational path, not an exotic edge case.
- **Deliberate exploitation**: An unprivileged actor can create a subscription (no registration required, only `msg.value ≥ minimumBalance`), configure a 1-second heartbeat, and wait for the balance to enter the stuck range. The `getActiveSubscriptions` function is public and permissionless, so keepers will discover and repeatedly attempt to service the subscription. [4](#0-3) 

---

### Recommendation

Add a conservative pre-check for the minimum possible keeper fee before performing any expensive work. The minimum keeper fee can be lower-bounded as `GAS_OVERHEAD * tx.gasprice + singleUpdateKeeperFeeInWei * numPriceIds`:

```solidity
uint256 pythFee = pyth.getUpdateFee(updateData);
uint256 minKeeperFee = (GAS_OVERHEAD * tx.gasprice)
    + (uint256(_state.singleUpdateKeeperFeeInWei) * params.priceIds.length);

if (status.balanceInWei < pythFee + minKeeperFee) {
    revert SchedulerErrors.InsufficientBalance();
}
```

This mirrors the pattern already used in `_processFeesAndPayKeeper` and ensures the subscription is solvent for the full cost before any gas-intensive work begins.

---

### Proof of Concept

1. Deploy `Scheduler` with `minimumBalancePerFeed = X` and `singleUpdateKeeperFeeInWei = K`.
2. Attacker calls `createSubscription` with 1 price feed, `heartbeatSeconds = 1`, funding exactly `minimumBalance = X`.
3. A keeper calls `updatePriceFeeds` repeatedly (heartbeat is 1 s). Each successful call deducts `pythFee + keeperFee` from the balance.
4. After enough updates, `balance` falls into `[pythFee, pythFee + GAS_OVERHEAD * gasprice + K)`.
5. From this point, every keeper call:
   - Passes line 295 (`balance >= pythFee` ✓)
   - Deducts `pythFee`, calls `pyth.parsePriceFeedUpdatesWithConfig` (expensive external call)
   - Runs `_validateShouldUpdatePrices` and `_storePriceUpdates`
   - Hits `_processFeesAndPayKeeper` → `balance < totalKeeperFee` → **revert**
   - All state is rolled back; balance is restored to the same stuck value
6. The subscription remains listed as active. Every keeper that polls it loses gas. The attacker's cost is fixed at `minimumBalance`; keeper losses accumulate indefinitely. [5](#0-4) [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L275-348)
```text
    function updatePriceFeeds(
        uint256 subscriptionId,
        bytes[] calldata updateData
    ) external override {
        uint256 startGas = gasleft();

        SchedulerStructs.SubscriptionStatus storage status = _state
            .subscriptionStatuses[subscriptionId];
        SchedulerStructs.SubscriptionParams storage params = _state
            .subscriptionParams[subscriptionId];

        if (!params.isActive) {
            revert SchedulerErrors.InactiveSubscription();
        }

        // Get the Pyth contract and parse price updates
        IPyth pyth = IPyth(_state.pyth);
        uint256 pythFee = pyth.getUpdateFee(updateData);

        // If we don't have enough balance, revert
        if (status.balanceInWei < pythFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
        status.totalSpent += pythFee;
        uint64 curTime = SafeCast.toUint64(block.timestamp);
        (
            PythStructs.PriceFeed[] memory priceFeeds,
            uint64[] memory slots
        ) = pyth.parsePriceFeedUpdatesWithConfig{value: pythFee}(
                updateData,
                params.priceIds,
                0, // We enforce the past max validity ourselves in _validateShouldUpdatePrices
                curTime + FUTURE_TIMESTAMP_MAX_VALIDITY_PERIOD,
                false,
                true,
                false
            );

        // Verify all price feeds have the same Pythnet slot.
        // All feeds in a subscription must be updated at the same time.
        uint64 slot = slots[0];
        for (uint8 i = 1; i < slots.length; i++) {
            if (slots[i] != slot) {
                revert SchedulerErrors.PriceSlotMismatch();
            }
        }

        // Verify that update conditions are met, and that the timestamp
        // is more recent than latest stored update's. Reverts if not.
        uint256 latestPublishTime = _validateShouldUpdatePrices(
            subscriptionId,
            params,
            status,
            priceFeeds
        );

        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;

        _storePriceUpdates(subscriptionId, priceFeeds);

        _processFeesAndPayKeeper(status, startGas, params.priceIds.length);

        emit PricesUpdated(subscriptionId, latestPublishTime);
    }
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L683-730)
```text
    // This function is intentionally public with no access control to allow keepers to discover active subscriptions
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L840-864)
```text
    function _processFeesAndPayKeeper(
        SchedulerStructs.SubscriptionStatus storage status,
        uint256 startGas,
        uint256 numPriceIds
    ) internal {
        // Calculate fee components
        uint256 gasCost = (startGas - gasleft() + GAS_OVERHEAD) * tx.gasprice;
        uint256 keeperSpecificFee = uint256(_state.singleUpdateKeeperFeeInWei) *
            numPriceIds;
        uint256 totalKeeperFee = gasCost + keeperSpecificFee;

        // Check balance
        if (status.balanceInWei < totalKeeperFee) {
            revert SchedulerErrors.InsufficientBalance();
        }

        status.balanceInWei -= totalKeeperFee;
        status.totalSpent += totalKeeperFee;

        // Pay keeper and update status
        (bool sent, ) = msg.sender.call{value: totalKeeperFee}("");
        if (!sent) {
            revert SchedulerErrors.KeeperPaymentFailed();
        }
    }
```
