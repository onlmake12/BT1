### Title
`getPricesNoOlderThan` / `getEmaPricesNoOlderThan` Staleness Check Uses Subscription-Level Max Timestamp Instead of Per-Feed `publishTime`, Allowing Stale Prices to Pass Freshness Guard - (File: `target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

The Scheduler contract's `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` functions check staleness against `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the subscription's last update batch. However, individual feeds in the same subscription can have significantly older `publishTime` values (e.g., closed-market equity or commodity feeds). A reader calling `getPricesNoOlderThan(subscriptionId, [GOLD_USD], 60)` expects all returned prices to be no older than 60 seconds, but may receive a price whose actual `publishTime` is hours or days old, because the staleness gate only validates the max timestamp of the batch.

---

### Finding Description

In `_validateShouldUpdatePrices`, `priceLastUpdatedAt` is set to the **maximum** `publishTime` across all feeds in the update:

```solidity
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
```

The comment explicitly acknowledges this: *"Use the most recent timestamp, as some asset markets may be closed. Closed markets will have a publishTime from their last trading period."* [1](#0-0) 

`status.priceLastUpdatedAt` is then stored as this max value: [2](#0-1) 

`getPricesNoOlderThan` then checks only this subscription-level max timestamp:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();

prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) 

`getPricesUnsafe` returns the raw stored `priceFeed.price` for each requested feed, which carries its own individual `publishTime` — potentially far older than `age_seconds`. [4](#0-3) 

The same flaw exists in `getEmaPricesNoOlderThan`: [5](#0-4) 

---

### Impact Explanation

A subscription containing a mix of high-frequency feeds (e.g., BTC/USD, updating every second) and low-frequency or session-based feeds (e.g., GOLD/USD, AAPL/USD, updating only during market hours) will have `priceLastUpdatedAt` driven by the most recently updated feed. When a reader calls `getPricesNoOlderThan(subscriptionId, [GOLD_USD_ID], 60)`:

1. The staleness gate passes because `priceLastUpdatedAt` reflects BTC/USD's recent timestamp.
2. The returned GOLD/USD price has a `publishTime` from the last market session — potentially 16+ hours ago.
3. The caller receives a `PythStructs.Price` struct with a stale price and no revert, violating the function's documented guarantee.

Any DeFi protocol (lending, derivatives, liquidation engine) consuming prices via `getPricesNoOlderThan` and relying on the freshness guarantee for session-based assets will operate on stale prices, potentially enabling incorrect liquidations, mispriced collateral, or exploitable arbitrage.

---

### Likelihood Explanation

- The Scheduler is explicitly designed to support mixed subscriptions with closed-market feeds (the comment in `_validateShouldUpdatePrices` confirms this is an intended use case).
- Any whitelisted reader (or any reader if `whitelistEnabled = false`) can call `getPricesNoOlderThan` — no privileged access required.
- The `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` constant is only 1 hour, meaning a subscription update can succeed with a feed whose `publishTime` is up to 1 hour old (if the max timestamp is recent). For equity/commodity feeds with 24-hour heartbeats, the stored price can be far older than any reasonable `age_seconds` passed by a consumer. [6](#0-5) 

---

### Recommendation

Replace the subscription-level `priceLastUpdatedAt` check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed `publishTime` check. After fetching the feeds via `_getPricesInternal`, iterate over each returned feed and verify its individual `publishTime` satisfies the `age_seconds` constraint:

```solidity
function getPricesNoOlderThan(...) external view ... {
    PythStructs.PriceFeed[] memory priceFeeds = _getPricesInternal(subscriptionId, priceIds);
    prices = new PythStructs.Price[](priceFeeds.length);
    for (uint i = 0; i < priceFeeds.length; i++) {
        if (distance(block.timestamp, priceFeeds[i].price.publishTime) > age_seconds)
            revert PythErrors.StalePrice();
        prices[i] = priceFeeds[i].price;
    }
}
```

This ensures the freshness guarantee is upheld per-feed, consistent with how `AbstractPyth.getPriceNoOlderThan` works for the core Pyth contract. [7](#0-6) 

---

### Proof of Concept

1. Create a subscription with two feeds: `BTC_USD` (1-second heartbeat) and `GOLD_USD` (session-based, ~24-hour heartbeat).
2. Submit an update batch where `BTC_USD.publishTime = block.timestamp` and `GOLD_USD.publishTime = block.timestamp - 20 hours`. Both feeds share the same Pythnet slot, so `PriceSlotMismatch` does not revert. `priceLastUpdatedAt` is set to `block.timestamp` (the max).
3. Call `getPricesNoOlderThan(subscriptionId, [GOLD_USD_ID], 60)`.
4. The check `distance(block.timestamp, priceLastUpdatedAt) > 60` evaluates to `distance(T, T) = 0 > 60` → **false** → no revert.
5. The returned `Price` for GOLD/USD has `publishTime = block.timestamp - 20 hours` — 72,000 seconds stale — yet the call succeeds without error. [8](#0-7) [9](#0-8)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L339-341)
```text
        // Update status and store the updates
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L362-386)
```text
        // Use the most recent timestamp, as some asset markets may be closed.
        // Closed markets will have a publishTime from their last trading period.
        // Since we verify all updates share the same Pythnet slot, we still ensure
        // that all price feeds are synchronized from the same update cycle.
        uint256 updateTimestamp = 0;
        for (uint8 i = 0; i < priceFeeds.length; i++) {
            if (priceFeeds[i].price.publishTime > updateTimestamp) {
                updateTimestamp = priceFeeds[i].price.publishTime;
            }
        }

        // Calculate the minimum acceptable timestamp (clamped at 0)
        // The maximum acceptable timestamp is enforced by the parsePriceFeedUpdatesWithSlots call
        uint256 minAllowedTimestamp = (block.timestamp >
            PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            ? (block.timestamp - PAST_TIMESTAMP_MAX_VALIDITY_PERIOD)
            : 0;

        // Validate that the update timestamp is not too old
        if (updateTimestamp < minAllowedTimestamp) {
            revert SchedulerErrors.TimestampTooOld(
                updateTimestamp,
                block.timestamp
            );
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L535-555)
```text
    function getPricesNoOlderThan(
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

**File:** target_chains/ethereum/pulse_sdk/solidity/SchedulerConstants.sol (L22-22)
```text
    uint64 public constant PAST_TIMESTAMP_MAX_VALIDITY_PERIOD = 1 hours;
```

**File:** target_chains/ethereum/sdk/solidity/AbstractPyth.sol (L50-60)
```text
    function getPriceNoOlderThan(
        bytes32 id,
        uint age
    ) public view virtual override returns (PythStructs.Price memory price) {
        price = getPriceUnsafe(id);

        if (diff(block.timestamp, price.publishTime) > age)
            revert PythErrors.StalePrice();

        return price;
    }
```
