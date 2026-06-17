### Title
`getPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp Instead of Per-Feed `publishTime` — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

### Summary

`Scheduler.getPricesNoOlderThan` validates freshness against a single subscription-level aggregate timestamp (`status.priceLastUpdatedAt`), which equals the **maximum** `publishTime` across all feeds in the last update batch. Individual price feeds with significantly older `publishTime` values (e.g., closed-market equity feeds) pass the staleness check and are returned to callers who believe all prices satisfy the `age_seconds` freshness guarantee.

### Finding Description

In `updatePriceFeeds`, the subscription status timestamp is set to the maximum `publishTime` across all feeds:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
// ...
status.priceLastUpdatedAt = latestPublishTime; // = max publishTime
``` [1](#0-0) [2](#0-1) 

`getPricesNoOlderThan` then checks only this single aggregate timestamp:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) 

The same pattern exists in `getEmaPricesNoOlderThan`: [4](#0-3) 

The contract's own documentation explicitly acknowledges that closed-market feeds carry timestamps from their last trading period, which can be hours or days old:

> "We don't want to reject update data if it contains a price from a market that closed a few days ago, since it will contain a timestamp from the last trading period." [5](#0-4) 

This means a subscription containing both a crypto feed (publishTime = now − 30s) and an equity feed (publishTime = 8 hours ago, market closed) will have `status.priceLastUpdatedAt = now − 30s`. A caller invoking `getPricesNoOlderThan(..., 60)` passes the staleness check and receives the equity price believing it is no older than 60 seconds, when it is actually 8 hours old.

The `getPricesUnsafe` path that is ultimately called returns raw stored prices with no per-feed age validation: [6](#0-5) 

The README recommends `getPricesNoOlderThan` as the safe, freshness-validated API for readers:

> "Readers are recommended to use the SDK's functions `get(Ema)PricesNoOlderThan`, which wrap the contract's `get(Ema)PricesUnsafe` functions and validate that the price is recent." [7](#0-6) 

### Impact Explanation

Protocols that use `getPricesNoOlderThan` as their freshness guard — for collateral valuation, liquidation thresholds, or settlement prices — will silently receive stale prices for any closed-market feed in the subscription. The function name and documentation create a false guarantee: callers believe all returned prices satisfy `age_seconds`, but individual feed `publishTime` values may be arbitrarily older. This can cause:

- Incorrect collateral valuations using stale equity/commodity prices
- Wrong liquidation amounts or missed liquidations
- Incorrect settlement prices in derivative protocols

The `SubscriptionStatus.priceLastUpdatedAt` field is documented as "Timestamp of the last update. All feeds in the subscription are updated together," reinforcing the false impression that all feeds share the same freshness level. [8](#0-7) 

### Likelihood Explanation

Mixed-asset subscriptions (crypto + equity/commodity) are a primary use case for Pulse/Scheduler. Any such subscription will routinely have feeds with divergent `publishTime` values whenever equity markets are closed. A permissionless keeper calling `updatePriceFeeds` with valid Pythnet data triggers the condition without any malicious intent. The vulnerability is triggered in normal operation, not only under adversarial conditions.

### Recommendation

Replace the subscription-level aggregate staleness check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed check against each individual price's `publishTime`:

```solidity
function getPricesNoOlderThan(
    uint256 subscriptionId,
    bytes32[] calldata priceIds,
    uint256 age_seconds
) external view override onlyWhitelistedReader(subscriptionId)
    returns (PythStructs.Price[] memory prices)
{
    PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(
        subscriptionId,
        priceIds
    );
    prices = new PythStructs.Price[](priceFeeds.length);
    for (uint i = 0; i < priceFeeds.length; i++) {
        if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
            revert PythErrors.StalePrice();
        prices[i] = priceFeeds[i].price;
    }
}
```

Alternatively, document clearly that `getPricesNoOlderThan` only guarantees that the **most recent** feed in the subscription is within `age_seconds`, and that callers must independently validate `publishTime` on each returned price for mixed-asset subscriptions.

### Proof of Concept

```solidity
// Scenario: subscription with BTC (crypto) + SPX (equity, market closed)
// 1. Keeper calls updatePriceFeeds with valid Pythnet data:
//    - BTC: publishTime = block.timestamp - 30
//    - SPX: publishTime = block.timestamp - 28800  (8 hours ago, market closed)
//    Both from the same Pythnet slot (slot check passes).
//
// 2. Inside updatePriceFeeds:
//    updateTimestamp = max(block.timestamp-30, block.timestamp-28800)
//                    = block.timestamp - 30
//    status.priceLastUpdatedAt = block.timestamp - 30
//
// 3. Reader calls getPricesNoOlderThan(subscriptionId, [BTC_ID, SPX_ID], 60):
//    distance(block.timestamp, block.timestamp - 30) = 30 <= 60  → NO REVERT
//    Returns both BTC price (30s old) and SPX price (8 hours old).
//
// 4. Reader protocol uses SPX price believing it is ≤ 60 seconds old.
//    Actual SPX publishTime age: 28800 seconds.
//
// Expected: StalePrice revert for SPX feed.
// Actual:   Both prices returned, SPX staleness silently bypassed.
```

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L299-305)
```text
        // Parse the price feed updates with an acceptable timestamp range of [0, now+10s].
        // Note: We don't want to reject update data if it contains a price
        // from a market that closed a few days ago, since it will contain a timestamp
        // from the last trading period. Thus, we use a minimum timestamp of zero while parsing,
        // and we enforce the past max validity ourselves in _validateShouldUpdatePrices using
        // the highest timestamp in the update data.
        status.balanceInWei -= pythFee;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L366-370)
```text
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
```

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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L546-555)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
    }
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

**File:** target_chains/ethereum/contracts/contracts/pulse/README.md (L56-57)
```markdown
7.  **Reading:** Readers get prices using the `@pythnetwork/pyth-sdk-solidity` SDK. Readers are recommended to use the SDK's functions `get(Ema)PricesNoOlderThan`, which wrap the contract's `get(Ema)PricesUnsafe` functions and validate that the price is recent.

```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L19-24)
```text
    struct SubscriptionStatus {
        uint256 priceLastUpdatedAt; // Timestamp of the last update. All feeds in the subscription are updated together.
        uint256 balanceInWei; // Balance that will be used to fund the subscription's upkeep.
        uint256 totalUpdates; // Tracks update count across all feeds in the subscription (increments by number of feeds per update)
        uint256 totalSpent; // Counter of total fees paid for subscription upkeep in wei.
    }
```
