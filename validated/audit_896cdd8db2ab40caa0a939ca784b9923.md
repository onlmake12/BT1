### Title
Unconstrained TWAP Window Selection in `parseTwapPriceFeedUpdates` Allows Near-Spot Price Submission — (File: `target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`, `target_chains/solana/programs/pyth-solana-receiver/src/lib.rs`)

---

### Summary

`parseTwapPriceFeedUpdates` (EVM) and `post_twap_update` (Solana) accept any two Wormhole-verified TWAP messages as start/end points without enforcing a minimum window size. An unprivileged transaction sender can cherry-pick a historically narrow window (as small as 1 slot, ≈400 ms on Solana) that coincides with a price spike or crash, producing a "TWAP" that is functionally equivalent to a spot price at that moment. Integrators that consume this output without independently validating the window length are exposed to the same class of attack as the OHM spot-price manipulation described in the reference report.

---

### Finding Description

`validateTwapPriceInfo` in `Pyth.sol` enforces only:

1. `prevPublishTime < publishTime` for each endpoint (uniqueness of each message)
2. Matching exponents
3. `startPublishSlot ≤ endPublishSlot` (non-strict `>` check — equal slots are accepted, which would also cause division-by-zero in `calculateTwap`)
4. `startPublishTime ≤ endPublishTime` [1](#0-0) 

There is **no minimum slot difference or minimum time-window requirement**. The analogous Solana validator `validate_twap_messages` does require `end_msg.publish_slot > start_msg.publish_slot` (strict), but also imposes no minimum gap. [2](#0-1) 

`calculateTwap` (EVM) and `calculate_twap` (Solana) then compute:

```
twapPrice = (cumulativePrice_end − cumulativePrice_start) / slotDiff
```

With `slotDiff = 1`, the result equals the instantaneous price at that single slot — a spot price, not a time-weighted average. [3](#0-2) [4](#0-3) 

The Solana SDK does provide `get_twap_no_older_than`, which checks that `actual_window == window_seconds`, but this check is **opt-in** and lives in the SDK, not in the on-chain program. The unsafe variant `get_twap_unchecked` explicitly warns that it allows "arbitrary window length twap updates." [5](#0-4) [6](#0-5) 

No equivalent window-enforcement helper exists in the EVM contract or its interface. [7](#0-6) 

---

### Impact Explanation

Any protocol that calls `parseTwapPriceFeedUpdates` (EVM) or reads a `TwapUpdate` account (Solana) without validating `startTime`/`endTime` can be fed a near-spot price disguised as a TWAP. This mirrors the OHM vulnerability exactly: the attacker does not need to manipulate the price on-chain; they only need to locate a historical Wormhole VAA pair (freely available via Hermes) that brackets a price extreme. The resulting "TWAP" can be used to:

- Inflate or deflate apparent collateral value in a lending protocol
- Trigger artificial liquidations or prevent legitimate ones
- Manipulate settlement prices in derivatives

The `downSlotsRatio` field is returned and could theoretically signal data quality, but it only measures missed slots, not window width, and no threshold is enforced by the contract. [8](#0-7) 

---

### Likelihood Explanation

Historical TWAP messages are publicly accessible via Hermes. An attacker needs only to:

1. Query Hermes for historical TWAP messages around a known price spike or flash crash.
2. Construct a valid Wormhole VAA pair (start = 1 slot before spike, end = spike slot).
3. Call `parseTwapPriceFeedUpdates` with that pair.

No privileged access, flash loan, or on-chain price manipulation is required. The attack is repeatable and low-cost (only the Pyth update fee is required). [9](#0-8) 

---

### Recommendation

**Short term:**
- Add a `minWindowSeconds` / `minSlotDiff` parameter to `parseTwapPriceFeedUpdates` and enforce it in `validateTwapPriceInfo`.
- Alternatively, revert if `slotDiff < MIN_TWAP_SLOTS` (e.g., 150 slots ≈ 1 minute on Solana).
- Document clearly that callers **must** validate `startTime` and `endTime` before using the returned price.

**Long term:**
- Mirror the Solana SDK's `get_twap_no_older_than` window-size check in the EVM contract itself, so the protection is on-chain rather than opt-in.
- Consider requiring a minimum window (e.g., 5 minutes) at the contract level to prevent near-spot abuse.

---

### Proof of Concept

**EVM (Solidity pseudocode):**

```solidity
// Attacker finds two historical Wormhole VAAs from Hermes:
//   startVAA: slot N,   cumulativePrice = C
//   endVAA:   slot N+1, cumulativePrice = C + spike_price
// slotDiff = 1  →  twapPrice = spike_price  (spot price at slot N+1)

bytes[] memory updateData = new bytes[](2);
updateData[0] = startVAA;   // slot N
updateData[1] = endVAA;     // slot N+1 (price spike)

PythStructs.TwapPriceFeed[] memory feeds =
    pyth.parseTwapPriceFeedUpdates{value: fee}(updateData, priceIds);
// feeds[0].twap.price == spike_price  (not a real TWAP)
// feeds[0].startTime and endTime differ by ~400ms — no revert occurs
```

The contract accepts this call because `validateTwapPriceInfo` only checks ordering, not minimum window width. [10](#0-9)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L491-506)
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
```

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L560-584)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L721-732)
```text
        // Calculate differences between start and end points for slots and cumulative values
        uint64 slotDiff = twapPriceInfoEnd.publishSlot -
            twapPriceInfoStart.publishSlot;
        int128 priceDiff = twapPriceInfoEnd.cumulativePrice -
            twapPriceInfoStart.cumulativePrice;
        uint128 confDiff = twapPriceInfoEnd.cumulativeConf -
            twapPriceInfoStart.cumulativeConf;

        // Calculate time-weighted average price (TWAP) and confidence by dividing
        // the difference in cumulative values by the number of slots between data points
        int128 twapPrice = priceDiff / int128(uint128(slotDiff));
        uint128 twapConf = confDiff / uint128(slotDiff);
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L526-555)
```rust
fn validate_twap_messages(start_msg: &TwapMessage, end_msg: &TwapMessage) -> Result<()> {
    // Validate feed ids match
    require!(
        start_msg.feed_id == end_msg.feed_id,
        ReceiverError::FeedIdMismatch
    );

    // Validate exponents match
    require!(
        start_msg.exponent == end_msg.exponent,
        ReceiverError::ExponentMismatch
    );

    // Validate slots
    require!(
        end_msg.publish_slot > start_msg.publish_slot,
        ReceiverError::InvalidTwapSlots
    );

    // Validate first messages in timestamp
    require!(
        start_msg.prev_publish_time < start_msg.publish_time,
        ReceiverError::InvalidTwapStartMessage
    );
    require!(
        end_msg.prev_publish_time < end_msg.publish_time,
        ReceiverError::InvalidTwapEndMessage
    );
    Ok(())
}
```

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L559-579)
```rust
fn calculate_twap(start_msg: &TwapMessage, end_msg: &TwapMessage) -> Result<(i64, u64, u32)> {
    let slot_diff = end_msg
        .publish_slot
        .checked_sub(start_msg.publish_slot)
        .ok_or(ReceiverError::TwapCalculationOverflow)?;

    let price_diff = end_msg
        .cumulative_price
        .checked_sub(start_msg.cumulative_price)
        .ok_or(ReceiverError::TwapCalculationOverflow)?;

    let conf_diff = end_msg
        .cumulative_conf
        .checked_sub(start_msg.cumulative_conf)
        .ok_or(ReceiverError::TwapCalculationOverflow)?;

    // Calculate time averaged price and confidence
    let price = i64::try_from(price_diff / i128::from(slot_diff))
        .map_err(|_| ReceiverError::TwapCalculationOverflow)?;
    let conf = u64::try_from(conf_diff / u128::from(slot_diff))
        .map_err(|_| ReceiverError::TwapCalculationOverflow)?;
```

**File:** target_chains/solana/pyth_solana_receiver_sdk/src/price_update.rs (L84-103)
```rust
    /// Get a `TwapPrice` from a `TwapUpdate` account for a given `FeedId`.
    ///
    /// # Warning
    /// This function does not check :
    /// - How recent the price is
    /// - If the TWAP's window size is expected
    /// - Whether the price update has been verified
    ///
    /// It is therefore unsafe to use this function without any extra checks,
    /// as it allows for the possibility of using unverified, outdated, or arbitrary window length twap updates.
    pub fn get_twap_unchecked(
        &self,
        feed_id: &FeedId,
    ) -> std::result::Result<TwapPrice, GetPriceError> {
        check!(
            self.twap.feed_id == *feed_id,
            GetPriceError::MismatchedFeedId
        );
        Ok(self.twap)
    }
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

**File:** target_chains/ethereum/sdk/solidity/IPyth.sol (L165-189)
```text
    /// @notice Parse time-weighted average price (TWAP) from two consecutive price updates for the given `priceIds`.
    ///
    /// This method calculates TWAP between two data points by processing the difference in cumulative price values
    /// divided by the time period. It requires exactly two updates that contain valid price information
    /// for all the requested price IDs.
    ///
    /// This method requires the caller to pay a fee in wei; the required fee can be computed by calling
    /// `getUpdateFee` with the updateData array.
    ///
    /// @dev Reverts if:
    /// - The transferred fee is not sufficient
    /// - The updateData is invalid or malformed
    /// - The updateData array does not contain exactly 2 updates
    /// - There is no update for any of the given `priceIds`
    /// - The time ordering between data points is invalid (start time must be before end time)
    /// @param updateData Array containing exactly two price updates (start and end points for TWAP calculation)
    /// @param priceIds Array of price ids to calculate TWAP for
    /// @return twapPriceFeeds Array of TWAP price feeds corresponding to the given `priceIds` (with the same order)
    function parseTwapPriceFeedUpdates(
        bytes[] calldata updateData,
        bytes32[] calldata priceIds
    )
        external
        payable
        returns (PythStructs.TwapPriceFeed[] memory twapPriceFeeds);
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
