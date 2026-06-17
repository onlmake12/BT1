### Title
`Scheduler.getPricesNoOlderThan()` Uses a Single Max-Timestamp to Validate All Feeds, Allowing Stale Individual Prices to Pass Freshness Checks — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.getPricesNoOlderThan()` and `getEmaPricesNoOlderThan()` validate freshness using a single subscription-level timestamp (`status.priceLastUpdatedAt`), which is the **maximum** `publishTime` across all feeds in the last update batch. When a subscription contains feeds with different natural update frequencies (e.g., a crypto feed alongside an equity feed that is closed overnight or on weekends), the freshness check passes based on the crypto feed's recent timestamp, while the equity feed's returned price may be many hours or days old.

---

### Finding Description

In `_validateShouldUpdatePrices`, the contract deliberately selects the **maximum** `publishTime` across all feeds as the representative update timestamp:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

This value is stored as `status.priceLastUpdatedAt`. [1](#0-0) 

`getPricesNoOlderThan` then checks this single max-timestamp against the caller-supplied `age_seconds`:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [2](#0-1) 

`getPricesUnsafe` returns each feed's stored `PriceFeed` struct, which carries its own individual `publishTime`. For a closed-market feed (e.g., AAPL/USD overnight), that `publishTime` may be 16+ hours in the past, yet the staleness check passes because `priceLastUpdatedAt` was set by a co-subscribed crypto feed (e.g., BTC/USD) that was updated seconds ago. [3](#0-2) 

The same flaw exists in `getEmaPricesNoOlderThan`: [4](#0-3) 

The `SubscriptionStatus` struct stores only one timestamp for the entire subscription, with no per-feed granularity: [5](#0-4) 

---

### Impact Explanation

A protocol integrating with Pulse calls `getPricesNoOlderThan(subscriptionId, [AAPL_USD, BTC_USD], 60)` expecting all returned prices to be no older than 60 seconds. The check passes (BTC/USD was updated 30 seconds ago), but the returned AAPL/USD price carries a `publishTime` from 16 hours ago (last market close). The protocol silently consumes a stale equity price, potentially enabling:

- Incorrect collateral valuation in lending protocols (stale equity collateral overvalued or undervalued)
- Incorrect liquidation decisions
- Arbitrage against the stale price before the market reopens

The function name `getPricesNoOlderThan` and its NatSpec documentation explicitly promise that returned prices are no older than `age_seconds`, creating a false security guarantee. [6](#0-5) 

---

### Likelihood Explanation

Pulse explicitly supports mixed-asset subscriptions (crypto + equities). The README and `SchedulerConstants` acknowledge that "some asset markets may be closed" and that closed-market feeds will carry timestamps from their last trading period. Any subscription combining a 24/7 crypto feed with a session-based equity feed triggers this condition every night and on weekends. A whitelisted reader (unprivileged, no special role required) is the direct entry point. [7](#0-6) [8](#0-7) 

---

### Recommendation

Replace the single-timestamp staleness check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed check against each feed's individual `publishTime`:

```solidity
function getPricesNoOlderThan(
    uint256 subscriptionId,
    bytes32[] calldata priceIds,
    uint256 age_seconds
) external view override onlyWhitelistedReader(subscriptionId) returns (PythStructs.Price[] memory prices) {
    prices = this.getPricesUnsafe(subscriptionId, priceIds);
    for (uint i = 0; i < prices.length; i++) {
        if (distance(block.timestamp, prices[i].publishTime) > age_seconds)
            revert PythErrors.StalePrice();
    }
}
```

This ensures every returned price individually satisfies the caller's freshness requirement, matching the function's documented contract.

---

### Proof of Concept

1. Create a subscription with two price IDs: `BTC_USD` (crypto, updates every second) and `AAPL_USD` (equity, last traded 16 hours ago).
2. A keeper calls `updatePriceFeeds`. Pyth parses both feeds from the same Pythnet slot. BTC/USD `publishTime = T`, AAPL/USD `publishTime = T - 57600` (16 hours). `priceLastUpdatedAt` is set to `T` (the max).
3. A whitelisted reader calls `getPricesNoOlderThan(subscriptionId, [AAPL_USD], 60)`.
4. The check `distance(block.timestamp, T) = 0 ≤ 60` passes.
5. The returned AAPL/USD price has `publishTime = T - 57600` — 16 hours stale — with no revert.

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L362-365)
```text
        // Use the most recent timestamp, as some asset markets may be closed.
        // Closed markets will have a publishTime from their last trading period.
        // Since we verify all updates share the same Pythnet slot, we still ensure
        // that all price feeds are synchronized from the same update cycle.
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L366-371)
```text
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L546-554)
```text
        SchedulerStructs.SubscriptionStatus memory status = _state
            .subscriptionStatuses[subscriptionId];

        // Use distance (absolute difference) since pythnet timestamps
        // may be slightly ahead of this chain.
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerStructs.sol (L19-24)
```text
    struct SubscriptionStatus {
        uint256 priceLastUpdatedAt; // Timestamp of the last update. All feeds in the subscription are updated together.
        uint256 balanceInWei; // Balance that will be used to fund the subscription's upkeep.
        uint256 totalUpdates; // Tracks update count across all feeds in the subscription (increments by number of feeds per update)
        uint256 totalSpent; // Counter of total fees paid for subscription upkeep in wei.
    }
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L68-77)
```text
    /// @notice Returns the price that is no older than `age` seconds of the current time.
    /// @dev This function is a sanity-checked version of `getPriceUnsafe` which is useful in
    /// applications that require a sufficiently-recent price. Reverts if the price wasn't updated sufficiently
    /// recently.
    /// @return prices - please read the documentation of PythStructs.Price to understand how to use this safely.
    function getPricesNoOlderThan(
        uint256 subscriptionId,
        bytes32[] calldata priceIds,
        uint256 age
    ) external view returns (PythStructs.Price[] memory prices);
```

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L14-22)
```text
    /// Maximum time in the past (relative to current block timestamp)
    /// for which a price update timestamp is considered valid
    /// when validating the update conditions.
    /// @dev Note: We don't use this when parsing update data from the Pyth contract
    /// because don't want to reject update data if it contains a price from a market
    /// that closed a few days ago, since it will contain a timestamp from the last
    /// trading period. We enforce this value ourselves against the maximum
    /// timestamp in the provided update data.
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```
