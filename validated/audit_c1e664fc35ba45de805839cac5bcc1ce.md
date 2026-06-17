### Title
`getPricesNoOlderThan` Uses Subscription-Level Max Timestamp Instead of Per-Feed Timestamps, Allowing Stale Individual Prices to Pass Freshness Checks — (`File: target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol`)

---

### Summary

`Scheduler.sol`'s `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` perform their staleness check against `status.priceLastUpdatedAt`, which is the **maximum** `publishTime` across all feeds in the last update batch. Individual feeds within the same subscription can have significantly older `publishTime` values. A consumer calling `getPricesNoOlderThan` with a tight `age_seconds` bound will receive stale per-feed prices while the contract's staleness guard silently passes.

---

### Finding Description

In `updatePriceFeeds`, the subscription-level timestamp is set to the maximum `publishTime` across all submitted feeds:

```solidity
// _validateShouldUpdatePrices
uint256 updateTimestamp = 0;
for (uint8 i = 0; i < priceFeeds.length; i++) {
    if (priceFeeds[i].price.publishTime > updateTimestamp) {
        updateTimestamp = priceFeeds[i].price.publishTime;
    }
}
``` [1](#0-0) 

This maximum is then stored as `priceLastUpdatedAt`:

```solidity
status.priceLastUpdatedAt = latestPublishTime;
``` [2](#0-1) 

`getPricesNoOlderThan` checks only this subscription-level maximum against `block.timestamp`, then delegates to `getPricesUnsafe` which returns raw per-feed prices with no individual timestamp validation:

```solidity
if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
    revert PythErrors.StalePrice();
prices = this.getPricesUnsafe(subscriptionId, priceIds);
``` [3](#0-2) 

The same flaw exists in `getEmaPricesNoOlderThan`: [4](#0-3) 

The Scheduler's own test suite explicitly confirms that an update batch containing one feed with a `stalePublishTime` (outside `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD`) and one feed with a `validPublishTime` succeeds, and `priceLastUpdatedAt` is set to `validPublishTime`: [5](#0-4) 

This means a consumer calling `getPricesNoOlderThan(subscriptionId, [stale_feed_id], 60)` will:
1. Pass the staleness guard (because `distance(block.timestamp, validPublishTime) ≤ 60`)
2. Receive the stale feed's price, whose individual `publishTime` may be hours old

The `IScheduler` interface documents `getPricesUnsafe` as returning prices "arbitrarily far in the past" and instructs callers to check `publishTime` themselves — but `getPricesNoOlderThan` is explicitly documented as the safe alternative that performs this check: [6](#0-5) 

---

### Impact Explanation

A consumer protocol that relies on `getPricesNoOlderThan` to enforce price freshness (e.g., a lending protocol checking that collateral prices are no older than 60 seconds) can silently receive prices that are hours old for individual feeds in a multi-feed subscription. This can lead to:

- Incorrect collateral valuations using stale prices
- Liquidations triggered or blocked based on outdated data
- Arbitrage against the protocol using known-stale prices

The `ConstantSourceConfig` in the off-chain `hip-3-pusher` service has the same structural issue (hardcoded price, no staleness check), but that is an off-chain operator tool and not a production smart contract reachable by unprivileged users: [7](#0-6) 

The on-chain `Scheduler.sol` issue is the production-scope finding.

---

### Likelihood Explanation

- **Realistic trigger**: Any unprivileged keeper can call `updatePriceFeeds`. Subscriptions with equity, commodity, or FX feeds alongside crypto feeds will routinely produce batches where some feeds have stale `publishTime` values (closed markets), while the crypto feed's `publishTime` is fresh. This is not a contrived edge case — the Scheduler is explicitly designed to handle it.
- **No privileged access required**: The keeper role is permissionless; anyone can submit a valid update.
- **Consumer impact is silent**: The consumer contract receives no revert and no signal that individual feed prices are stale. The `publishTime` field is present in the returned struct but many integrations do not re-check it after calling `getPricesNoOlderThan`.

---

### Recommendation

Replace the subscription-level timestamp check in `getPricesNoOlderThan` and `getEmaPricesNoOlderThan` with a per-feed timestamp check. After fetching the feeds via `_getPricesInternal`, iterate over each returned feed and verify that `distance(block.timestamp, feed.price.publishTime) <= age_seconds` before including it in the result (or revert if any feed fails). This mirrors the per-price staleness check used in the core Pyth EVM contract's `getPriceNoOlderThan`.

---

### Proof of Concept

1. Create a subscription with two price feeds: `feedA` (BTC, active market) and `feedB` (equity, closed market).
2. Submit `updatePriceFeeds` with:
   - `feedA.publishTime = block.timestamp` (fresh)
   - `feedB.publishTime = block.timestamp - 7200` (2 hours old, outside `PAST_TIMESTAMP_MAX_VALIDITY_PERIOD` of 1 hour, but the max is fresh so the update passes)
3. `status.priceLastUpdatedAt` is set to `feedA.publishTime`.
4. Call `getPricesNoOlderThan(subscriptionId, [feedB_id], 60)`:
   - Guard: `distance(block.timestamp, feedA.publishTime) = 0 ≤ 60` → **passes**
   - Returns `feedB` price with `publishTime = block.timestamp - 7200` — **2 hours stale**
5. The consumer contract receives a 2-hour-old price with no revert and no staleness signal.

The test `testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid` at line 2322 of `PulseScheduler.t.sol` already demonstrates step 2 succeeding in the test suite. [5](#0-4)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L340-341)
```text
        status.priceLastUpdatedAt = latestPublishTime;
        status.totalUpdates += priceFeeds.length;
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

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L551-554)
```text
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/contracts/contracts/pulse/Scheduler.sol (L594-597)
```text
        if (distance(block.timestamp, status.priceLastUpdatedAt) > age_seconds)
            revert PythErrors.StalePrice();

        prices = this.getEmaPricesUnsafe(subscriptionId, priceIds);
```

**File:** target_chains/ethereum/contracts/test/PulseScheduler.t.sol (L2322-2370)
```text
    function testUpdatePriceFeedsSucceedsWithStaleFeedIfLatestIsValid() public {
        // Add a subscription and funds
        uint256 subscriptionId = addTestSubscription(
            scheduler,
            address(reader)
        );

        // Advance time past the validity period
        vm.warp(
            block.timestamp +
                scheduler.PAST_TIMESTAMP_MAX_VALIDITY_PERIOD() +
                600
        ); // Warp 1 hour 10 mins

        uint64 currentTime = SafeCast.toUint64(block.timestamp);
        uint64 validPublishTime = currentTime - 1800; // 30 mins ago (within 1 hour validity)
        uint64 stalePublishTime = currentTime -
            (scheduler.PAST_TIMESTAMP_MAX_VALIDITY_PERIOD() + 300); // 1 hour 5 mins ago (outside validity)

        PythStructs.PriceFeed[] memory priceFeeds = new PythStructs.PriceFeed[](
            2
        );
        priceFeeds[0] = createSingleMockPriceFeed(stalePublishTime);
        priceFeeds[1] = createSingleMockPriceFeed(validPublishTime);

        uint64[] memory slots = new uint64[](2);
        slots[0] = 100;
        slots[1] = 100; // Same slot

        // Mock Pyth response (should succeed in the real world as minValidTime is 0)
        mockParsePriceFeedUpdatesWithSlotsStrict(pyth, priceFeeds, slots);
        bytes[] memory updateData = createMockUpdateData(priceFeeds);

        // Expect PricesUpdated event with the latest valid timestamp
        vm.expectEmit();
        emit PricesUpdated(subscriptionId, validPublishTime);

        // Perform update - should succeed because the latest timestamp in the update data is valid
        vm.prank(pusher);
        scheduler.updatePriceFeeds(subscriptionId, updateData);

        // Verify last updated timestamp
        (, SchedulerStructs.SubscriptionStatus memory status) = scheduler
            .getSubscription(subscriptionId);
        assertEq(
            status.priceLastUpdatedAt,
            validPublishTime,
            "Last updated timestamp should be the latest valid one"
        );
```

**File:** target_chains/ethereum/pulse_sdk/solidity/IScheduler.sol (L55-77)
```text
    /// @notice Returns the price of a price feed without any sanity checks.
    /// @dev This function returns the most recent price update in this contract without any recency checks.
    /// This function is unsafe as the returned price update may be arbitrarily far in the past.
    ///
    /// Users of this function should check the `publishTime` in the price to ensure that the returned price is
    /// sufficiently recent for their application. If you are considering using this function, it may be
    /// safer / easier to use `getPricesNoOlderThan`.
    /// @return prices - please read the documentation of PythStructs.Price to understand how to use this safely.
    function getPricesUnsafe(
        uint256 subscriptionId,
        bytes32[] calldata priceIds
    ) external view returns (PythStructs.Price[] memory prices);

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

**File:** apps/hip-3-pusher/src/pusher/price_state.py (L248-250)
```python
        if isinstance(price_source_config, ConstantSourceConfig):
            # Constants always return their value (no staleness check)
            return str(price_source_config.value)
```
