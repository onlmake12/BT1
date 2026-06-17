### Title
Missing Staleness and Window-Size Enforcement in `parseTwapPriceFeedUpdates` Allows Arbitrarily Old TWAP Data to Be Accepted — (File: `target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

---

### Summary

`parseTwapPriceFeedUpdates` in the EVM Pyth contract accepts any two cryptographically valid TWAP data points with no staleness check and no window-size constraint. Unlike `parsePriceFeedUpdates`, which exposes `minPublishTime`/`maxPublishTime` parameters so callers can enforce a time window on-chain, `parseTwapPriceFeedUpdates` provides no equivalent mechanism. An unprivileged transaction sender can supply valid but arbitrarily old TWAP data (freely available from the Benchmarks API) to any protocol that integrates this function, causing the protocol to settle or price positions using a stale, cherry-picked historical average.

---

### Finding Description

`parseTwapPriceFeedUpdates` validates only structural correctness of the two data points:

- Same number of price feeds and matching IDs
- Same exponent
- `startSlot < endSlot`
- `startTime ≤ endTime`
- `prevPublishTime < publishTime` for each point [1](#0-0) 

It performs **no check** against `block.timestamp`. There is no `minPublishTime`, `maxPublishTime`, or minimum/maximum window-size parameter. The function signature is:

```solidity
function parseTwapPriceFeedUpdates(
    bytes[] calldata updateData,
    bytes32[] calldata priceIds
) external payable returns (PythStructs.TwapPriceFeed[] memory twapPriceFeeds);
``` [2](#0-1) 

Compare this to `parsePriceFeedUpdates`, which requires the caller to bound the acceptable publish-time window:

```solidity
function parsePriceFeedUpdates(
    bytes[] calldata updateData,
    bytes32[] calldata priceIds,
    uint64 minPublishTime,
    uint64 maxPublishTime
) external payable returns (PythStructs.PriceFeed[] memory priceFeeds);
``` [3](#0-2) 

The Solana SDK explicitly provides `get_twap_no_older_than`, which enforces **both** a staleness bound and an exact window-size match:

```rust
let actual_window = twap_price.end_time.saturating_sub(twap_price.start_time);
check!(
    actual_window == i64::try_from(window_seconds).unwrap(),
    GetPriceError::InvalidWindowSize
);
``` [4](#0-3) 

The EVM contract has no equivalent. The `TwapPriceFeed` struct returns `startTime` and `endTime` fields, but the contract itself enforces nothing about their relationship to the current block time. [5](#0-4) 

---

### Impact Explanation

Any EVM protocol that calls `parseTwapPriceFeedUpdates` and uses the returned price for settlement, collateral valuation, or liquidation can be fed a TWAP computed over an arbitrary historical window. Because the Pyth pull model lets the transaction sender supply the `updateData`, an attacker can:

1. Retrieve valid, Wormhole-signed TWAP data from a past period (e.g., 6 months ago) via the public Benchmarks API.
2. Submit that data to a victim protocol's contract.
3. The victim contract calls `parseTwapPriceFeedUpdates`; the Pyth contract accepts it without complaint.
4. The victim protocol uses the stale, cherry-picked TWAP for pricing, enabling the attacker to profit from the price discrepancy.

The asymmetry with `parsePriceFeedUpdates` (which has `minPublishTime`/`maxPublishTime`) means integrators who follow the regular price-feed pattern may not realize they must add their own staleness checks for TWAP, making this a realistic integration pitfall with direct financial impact.

---

### Likelihood Explanation

- Historical TWAP data is publicly available from the Benchmarks API; no privileged access is required.
- The attacker only needs to be the transaction sender (or front-run a victim's transaction with their own `updateData`).
- The Pyth pull model explicitly allows the caller to supply `updateData`, making this the intended interaction pattern.
- Protocols that integrate `parseTwapPriceFeedUpdates` for settlement are the primary target; such protocols exist and are the stated use case for the TWAP API.

---

### Recommendation

Add `minPublishTime` and `maxPublishTime` (or equivalently `maxAge` and `windowSeconds`) parameters to `parseTwapPriceFeedUpdates`, mirroring the design of `parsePriceFeedUpdates`:

```solidity
function parseTwapPriceFeedUpdates(
    bytes[] calldata updateData,
    bytes32[] calldata priceIds,
    uint64 minWindowStart,   // earliest acceptable startTime
    uint64 maxWindowEnd,     // latest acceptable endTime (staleness bound)
    uint64 minWindowSeconds, // minimum acceptable (endTime - startTime)
    uint64 maxWindowSeconds  // maximum acceptable (endTime - startTime)
) external payable returns (PythStructs.TwapPriceFeed[] memory twapPriceFeeds);
```

At minimum, enforce that `endTime + maxAge >= block.timestamp` and that `endTime - startTime` falls within an expected range, consistent with the Solana SDK's `get_twap_no_older_than`. [4](#0-3) 

---

### Proof of Concept

1. Deploy a victim contract that calls `parseTwapPriceFeedUpdates` and uses the returned `twap.price` for settlement.
2. Retrieve valid Wormhole-signed TWAP accumulator data from 6 months ago via `https://benchmarks.pyth.network/v1/shims/tradingview/history` or the Hermes historical endpoint.
3. Call the victim contract's settlement function, supplying the old TWAP data as `updateData`.
4. `parseTwapPriceFeedUpdates` in `Pyth.sol` (lines 491–584) accepts the data — `validateTwapPriceInfo` only checks internal ordering, not age.
5. The victim contract receives a TWAP from 6 months ago and settles at that price, while the attacker holds a position sized to profit from the discrepancy between the historical and current price. [6](#0-5)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L491-584)
```text
    function parseTwapPriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    )
        external
        payable
        override
        returns (PythStructs.TwapPriceFeed[] memory twapPriceFeeds)
    {
        // TWAP requires exactly 2 updates: one for the start point and one for the end point
        if (updateData.length != 2) {
            revert PythErrors.InvalidUpdateData();
        }

        uint requiredFee = getTwapUpdateFee(updateData);
        if (msg.value < requiredFee) revert PythErrors.InsufficientFee();

        // Process start update data
        PythStructs.TwapPriceInfo[] memory startTwapPriceInfos;
        bytes32[] memory startPriceIds;
        {
            uint offsetStart;
            (
                offsetStart,
                startTwapPriceInfos,
                startPriceIds
            ) = extractTwapPriceInfos(updateData[0]);
        }

        // Process end update data
        PythStructs.TwapPriceInfo[] memory endTwapPriceInfos;
        bytes32[] memory endPriceIds;
        {
            uint offsetEnd;
            (offsetEnd, endTwapPriceInfos, endPriceIds) = extractTwapPriceInfos(
                updateData[1]
            );
        }

        // Verify that we have the same number of price feeds in start and end updates
        if (startPriceIds.length != endPriceIds.length) {
            revert PythErrors.InvalidTwapUpdateDataSet();
        }

        // Hermes always returns price feeds in the same order for start and end updates
        // This allows us to assume startPriceIds[i] == endPriceIds[i] for efficiency
        for (uint i = 0; i < startPriceIds.length; i++) {
            if (startPriceIds[i] != endPriceIds[i]) {
                revert PythErrors.InvalidTwapUpdateDataSet();
            }
        }

        // Initialize the output array
        twapPriceFeeds = new PythStructs.TwapPriceFeed[](priceIds.length);

        // For each requested price ID, find matching start and end data points
        for (uint i = 0; i < priceIds.length; i++) {
            bytes32 requestedPriceId = priceIds[i];
            int startIdx = -1;

            // Find the index of this price ID in the startPriceIds array
            // (which is the same as the endPriceIds array based on our validation above)
            for (uint j = 0; j < startPriceIds.length; j++) {
                if (startPriceIds[j] == requestedPriceId) {
                    startIdx = int(j);
                    break;
                }
            }

            // If we found the price ID
            if (startIdx >= 0) {
                uint idx = uint(startIdx);
                // Validate the pair of price infos
                validateTwapPriceInfo(
                    startTwapPriceInfos[idx],
                    endTwapPriceInfos[idx]
                );

                // Calculate TWAP from these data points
                twapPriceFeeds[i] = calculateTwap(
                    requestedPriceId,
                    startTwapPriceInfos[idx],
                    endTwapPriceInfos[idx]
                );
            }
        }

        // Ensure all requested price IDs were found
        for (uint k = 0; k < priceIds.length; k++) {
            if (twapPriceFeeds[k].id == 0) {
                revert PythErrors.PriceFeedNotFoundWithinRange();
            }
        }
    }
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L586-610)
```text
    function validateTwapPriceInfo(
        PythStructs.TwapPriceInfo memory twapPriceInfoStart,
        PythStructs.TwapPriceInfo memory twapPriceInfoEnd
    ) private pure {
        // First validate each individual price's uniqueness
        if (
            twapPriceInfoStart.prevPublishTime >= twapPriceInfoStart.publishTime
        ) {
            revert PythErrors.InvalidTwapUpdateData();
        }
        if (twapPriceInfoEnd.prevPublishTime >= twapPriceInfoEnd.publishTime) {
            revert PythErrors.InvalidTwapUpdateData();
        }

        // Then validate the relationship between the two data points
        if (twapPriceInfoStart.expo != twapPriceInfoEnd.expo) {
            revert PythErrors.InvalidTwapUpdateDataSet();
        }
        if (twapPriceInfoStart.publishSlot > twapPriceInfoEnd.publishSlot) {
            revert PythErrors.InvalidTwapUpdateDataSet();
        }
        if (twapPriceInfoStart.publishTime > twapPriceInfoEnd.publishTime) {
            revert PythErrors.InvalidTwapUpdateDataSet();
        }
    }
```

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L121-126)
```text
    function parsePriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds,
        uint64 minPublishTime,
        uint64 maxPublishTime
    ) external payable returns (PythStructs.PriceFeed[] memory priceFeeds);
```

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L183-189)
```text
    function parseTwapPriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    )
        external
        payable
        returns (PythStructs.TwapPriceFeed[] memory twapPriceFeeds);
```

**File:** target_chains/solana/pyth_solana_receiver_sdk/src/price_update.rs (L131-156)
```rust
    pub fn get_twap_no_older_than(
        &self,
        clock: &Clock,
        maximum_age: u64,
        window_seconds: u64,
        feed_id: &FeedId,
    ) -> std::result::Result<TwapPrice, GetPriceError> {
        // Ensure the update isn't outdated
        let twap_price = self.get_twap_unchecked(feed_id)?;
        check!(
            twap_price
                .end_time
                .saturating_add(maximum_age.try_into().unwrap())
                >= clock.unix_timestamp,
            GetPriceError::PriceTooOld
        );

        // Ensure the twap window size is as expected
        let actual_window = twap_price.end_time.saturating_sub(twap_price.start_time);
        check!(
            actual_window == i64::try_from(window_seconds).unwrap(),
            GetPriceError::InvalidWindowSize
        );

        Ok(twap_price)
    }
```

**File:** target_chains/ethereum/sdk/solidity/PythStructs.sol (L34-53)
```text
    struct TwapPriceFeed {
        // The price ID.
        bytes32 id;
        // Start time of the TWAP
        uint64 startTime;
        // End time of the TWAP
        uint64 endTime;
        // TWAP price
        Price twap;
        // Down slot ratio represents the ratio of price feed updates that were missed or unavailable
        // during the TWAP period, expressed as a fixed-point number between 0 and 1e6 (100%).
        // For example:
        //   - 0 means all price updates were available
        //   - 500_000 means 50% of updates were missed
        //   - 1_000_000 means all updates were missed
        // This can be used to assess the quality/reliability of the TWAP calculation.
        // Applications should define a maximum acceptable ratio (e.g. 100000 for 10%)
        // and revert if downSlotsRatio exceeds it.
        uint32 downSlotsRatio;
    }
```
