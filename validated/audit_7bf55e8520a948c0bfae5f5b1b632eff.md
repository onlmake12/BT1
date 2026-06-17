### Title
Deviation-Only Scheduler Subscriptions Permanently Block Price Updates When Asset Price Crashes to Zero — (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary
In `Scheduler.sol`, the `_validateShouldUpdatePrices` function silently skips the deviation check for any feed whose current or previous price is zero (`continue`). When a subscription is configured with `updateOnDeviation` as its **sole** update criterion and every tracked feed reports a current price of zero, all feeds are skipped, the loop exits without returning, and the function reverts with `UpdateConditionsNotMet`. The Scheduler permanently refuses to store the zero price, leaving stale non-zero prices in its internal state indefinitely.

### Finding Description

`_validateShouldUpdatePrices` in `Scheduler.sol` evaluates two independent criteria in sequence: heartbeat and deviation.

```
// If updateOnHeartbeat is enabled and the heartbeat interval has passed, trigger update
if (params.updateCriteria.updateOnHeartbeat) { ... return updateTimestamp; }

// If updateOnDeviation is enabled, check if any price has deviated enough
if (params.updateCriteria.updateOnDeviation) {
    for (uint8 i = 0; i < priceFeeds.length; i++) {
        ...
        int64 currentPrice = priceFeeds[i].price.price;
        int64 previousPrice = previousFeed.price.price;

        // Skip if either price is zero to avoid division by zero
        if (previousPrice == 0 || currentPrice == 0) {
            continue;                          // ← silently skips the feed
        }
        ...
        if (deviationBps >= params.updateCriteria.deviationThresholdBps) {
            return updateTimestamp;
        }
    }
}

revert SchedulerErrors.UpdateConditionsNotMet();   // ← reached when all feeds skipped
```

When `updateOnHeartbeat = false` and `updateOnDeviation = true`, the heartbeat branch is never entered. If every feed in the subscription has `currentPrice == 0` (a complete price collapse), every iteration of the deviation loop hits `continue`, the loop terminates without returning, and the function reverts. The Pyth fee has already been deducted from `status.balanceInWei` before this revert path is reached, so the subscription also loses funds on every failed attempt.

The `updatePriceFeeds` entry point is fully permissionless — any address may call it — so there is no privileged gating that would prevent this state from being reached.

### Impact Explanation

Protocols that subscribe to the Scheduler with deviation-only criteria and read prices via `getPrices` / `getPricesUnsafe` will observe the last non-zero price indefinitely after a complete price collapse. Any DeFi protocol (lending, derivatives, liquidation engine) that relies on the Scheduler's stored price rather than querying the underlying Pyth contract directly will use a materially incorrect price. This creates the same arbitrage window described in the reference report: users can trade against the stale price until the protocol is drained or the subscription is manually reconfigured.

Additionally, every `updatePriceFeeds` call that reaches the revert path still deducts the Pyth parsing fee from the subscription balance, draining the subscription's ETH reserve and eventually causing the subscription to become underfunded.

### Likelihood Explanation

Pyth price feeds use `int64` for the price field. A price of exactly zero is a valid on-chain value and can be published by the Pythnet aggregator for assets that have lost all market value (e.g., a depegged stablecoin, a rug-pulled token, or a synthetic asset whose collateral has been fully liquidated). The condition is reachable without any attacker action: it is triggered by the normal Pyth price publication pipeline reporting a zero price. Any subscription that was created with `updateOnDeviation` only (no heartbeat) — a configuration the contract explicitly permits and the SDK documentation shows as a valid option — is permanently affected once this state is reached.

### Recommendation

When `currentPrice == 0`, treat the move from any non-zero `previousPrice` as a 100 % (10 000 bps) deviation and trigger the update unconditionally, rather than skipping the feed:

```solidity
if (previousPrice == 0) {
    continue; // still skip division by zero on denominator
}
if (currentPrice == 0) {
    return updateTimestamp; // 100% drop always exceeds any threshold
}
```

Additionally, consider requiring that at least one of `updateOnHeartbeat` or `updateOnDeviation` is always paired with the other, so that a heartbeat backstop guarantees eventual updates regardless of price magnitude.

### Proof of Concept

1. Deploy `Scheduler` and create a subscription with:
   - `updateOnHeartbeat = false`
   - `updateOnDeviation = true`, `deviationThresholdBps = 100` (1 %)
2. Call `updatePriceFeeds` with a valid Pyth update containing `price = 1000` for all feeds. This succeeds and stores `previousPrice = 1000`.
3. The tracked asset collapses; Pyth now publishes `price = 0`.
4. Call `updatePriceFeeds` with the new Pyth update containing `price = 0`.
   - `parsePriceFeedUpdatesWithConfig` succeeds and returns the zero-price feeds.
   - `_validateShouldUpdatePrices` enters the deviation loop.
   - For every feed: `currentPrice == 0` → `continue`.
   - Loop exits; function hits `revert UpdateConditionsNotMet()`.
5. The Scheduler's stored price remains `1000` while the true price is `0`.
6. Any protocol reading `getPrices(subscriptionId, ...)` receives the stale `1000` price and can be exploited accordingly. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L412-453)
```text
        // If updateOnDeviation is enabled, check if any price has deviated enough
        if (params.updateCriteria.updateOnDeviation) {
            for (uint8 i = 0; i < priceFeeds.length; i++) {
                // Get the previous price feed for this price ID using subscriptionId
                PythStructs.PriceFeed storage previousFeed = _state
                    .priceUpdates[subscriptionId][priceFeeds[i].id];

                // If there's no previous price, this is the first update
                if (previousFeed.id == bytes32(0)) {
                    return updateTimestamp;
                }

                // Calculate the deviation percentage
                int64 currentPrice = priceFeeds[i].price.price;
                int64 previousPrice = previousFeed.price.price;

                // Skip if either price is zero to avoid division by zero
                if (previousPrice == 0 || currentPrice == 0) {
                    continue;
                }

                // Calculate absolute deviation basis points (scaled by 1e4)
                uint256 numerator = SignedMath.abs(
                    currentPrice - previousPrice
                );
                uint256 denominator = SignedMath.abs(previousPrice);
                uint256 deviationBps = Math.mulDiv(
                    numerator,
                    10_000,
                    denominator
                );

                // If deviation exceeds threshold, trigger update
                if (
                    deviationBps >= params.updateCriteria.deviationThresholdBps
                ) {
                    return updateTimestamp;
                }
            }
        }

        revert SchedulerErrors.UpdateConditionsNotMet();
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L27-32)
```text
    struct UpdateCriteria {
        bool updateOnHeartbeat; // Should update based on time elapsed
        uint32 heartbeatSeconds; // Time interval for heartbeat updates
        bool updateOnDeviation; // Should update based on price deviation
        uint32 deviationThresholdBps; // Price deviation threshold in basis points
    }
```
