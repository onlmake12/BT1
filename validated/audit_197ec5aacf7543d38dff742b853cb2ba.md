### Title
No Minimum TWAP Window Size Enforced — Caller Can Submit 1-Slot Window, Degrading TWAP to a Spot Price - (File: `target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

---

### Summary

`parseTwapPriceFeedUpdates` (EVM) and `post_twap_update` (Solana) impose no minimum window size on the TWAP interval. An unprivileged transaction sender can supply two Wormhole-attested TWAP messages that are only 1 slot apart, causing the protocol to return a near-spot price labeled as a TWAP. This is the direct analog of `periodSize = 0` in the referenced report: the averaging window collapses to a single observation, stripping the TWAP of its manipulation-resistance property.

---

### Finding Description

Pyth's TWAP is computed as:

```
twapPrice = (cumulativePrice_end − cumulativePrice_start) / slotDiff
```

The only slot-ordering check in `validateTwapPriceInfo` (EVM) is:

```solidity
// Pyth.sol line 604
if (twapPriceInfoStart.publishSlot > twapPriceInfoEnd.publishSlot) {
    revert PythErrors.InvalidTwapUpdateDataSet();
}
``` [1](#0-0) 

This uses strict `>`, so equal slots pass validation (though they cause a division-by-zero revert in `calculateTwap`). Critically, **`slotDiff = 1` is fully accepted** — no minimum window is enforced. The Solana counterpart `validate_twap_messages` has the same gap:

```rust
// lib.rs line 540-543
require!(
    end_msg.publish_slot > start_msg.publish_slot,
    ReceiverError::InvalidTwapSlots
);
``` [2](#0-1) 

With `slotDiff = 1`, `calculateTwap` reduces to:

```solidity
// Pyth.sol lines 731-732
int128 twapPrice = priceDiff / int128(uint128(slotDiff)); // slotDiff = 1 → spot price
uint128 twapConf  = confDiff / uint128(slotDiff);
``` [3](#0-2) 

The result is mathematically identical to the spot price at that single slot. The `downSlotsRatio` field is computed but **never enforced on-chain**; the struct comment explicitly delegates this check to the application:

> "Applications should define a maximum acceptable ratio (e.g. 100000 for 10%) and revert if downSlotsRatio exceeds it." [4](#0-3) 

The Solana SDK's `get_twap_no_older_than` does enforce an exact `window_seconds`, but `get_twap_unchecked` explicitly does not, and the on-chain program itself enforces nothing: [5](#0-4) 

---

### Impact Explanation

Consumer protocols (lending markets, perpetuals, derivatives) that call `parseTwapPriceFeedUpdates` expecting a manipulation-resistant time-averaged price receive instead a near-spot price from a cherry-picked 1-slot window. This defeats the core purpose of TWAP. A lending protocol using TWAP for collateral valuation could be exploited: an attacker submits a 1-slot window that captures a momentary price spike, inflating collateral value and enabling over-borrowing or under-liquidation.

---

### Likelihood Explanation

The entry path is fully unprivileged. Any transaction sender can call `parseTwapPriceFeedUpdates` with any two valid Wormhole-attested TWAP messages obtained from Hermes. The attacker does not need to manipulate Pyth prices — they only need to **select** a favorable 1-slot window from the historical stream of attested messages. Natural price volatility (which occurs regularly in crypto markets) provides the necessary momentary price deviation. The only cost is the update fee.

---

### Recommendation

Enforce a minimum slot difference (and/or minimum time difference) in `validateTwapPriceInfo` (EVM) and `validate_twap_messages` (Solana). For example:

```solidity
// EVM: Pyth.sol validateTwapPriceInfo
uint64 MIN_TWAP_SLOTS = 300; // ~2.5 minutes on Pythnet at ~0.5s/slot
if (twapPriceInfoEnd.publishSlot - twapPriceInfoStart.publishSlot < MIN_TWAP_SLOTS) {
    revert PythErrors.InvalidTwapUpdateDataSet();
}
```

```rust
// Solana: validate_twap_messages
const MIN_TWAP_SLOTS: u64 = 300;
require!(
    end_msg.publish_slot.saturating_sub(start_msg.publish_slot) >= MIN_TWAP_SLOTS,
    ReceiverError::InvalidTwapSlots
);
```

Additionally, consider enforcing an on-chain maximum `downSlotsRatio` threshold rather than leaving it entirely to consumers.

---

### Proof of Concept

1. Fetch two consecutive Wormhole-attested TWAP messages for the same feed from Hermes where `end.publishSlot = start.publishSlot + 1`.
2. Call `parseTwapPriceFeedUpdates(updateData, priceIds)` on the EVM Pyth contract with these two messages.
3. `validateTwapPriceInfo` passes: exponents match, `startSlot < endSlot`, `startTime ≤ endTime`, `prevPublishTime < publishTime` for both.
4. `calculateTwap` computes `slotDiff = 1`, so `twapPrice = priceDiff / 1 = cumulativePrice_end − cumulativePrice_start` — the exact price accumulated in that single slot.
5. The returned `TwapPriceFeed.twap.price` is a spot price, not a time-weighted average.
6. A consumer protocol that does not independently validate `endTime − startTime` accepts this as a valid TWAP, exposing itself to price manipulation via cherry-picked 1-slot windows. [6](#0-5) [7](#0-6)

### Citations

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L600-610)
```text
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

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L712-754)
```text
    function calculateTwap(
        bytes32 priceId,
        PythStructs.TwapPriceInfo memory twapPriceInfoStart,
        PythStructs.TwapPriceInfo memory twapPriceInfoEnd
    ) private pure returns (PythStructs.TwapPriceFeed memory twapPriceFeed) {
        twapPriceFeed.id = priceId;
        twapPriceFeed.startTime = twapPriceInfoStart.publishTime;
        twapPriceFeed.endTime = twapPriceInfoEnd.publishTime;

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

        // The conversion from int128 to int64 is safe because:
        // 1. Individual prices fit within int64 by protocol design
        // 2. TWAP is essentially an average price over time (cumulativePrice₂-cumulativePrice₁)/slotDiff
        // 3. This average must be within the range of individual prices that went into the calculation
        // We use int128 only as an intermediate type to safely handle cumulative sums
        twapPriceFeed.twap.price = int64(twapPrice);
        twapPriceFeed.twap.conf = uint64(twapConf);
        twapPriceFeed.twap.expo = twapPriceInfoStart.expo;
        twapPriceFeed.twap.publishTime = twapPriceInfoEnd.publishTime;

        // Calculate downSlotsRatio as a value between 0 and 1,000,000
        // 0 means no slots were missed, 1,000,000 means all slots were missed
        uint64 totalDownSlots = twapPriceInfoEnd.numDownSlots -
            twapPriceInfoStart.numDownSlots;
        uint64 downSlotsRatio = (totalDownSlots * 1_000_000) / slotDiff;

        // Safely downcast to uint32 (sufficient for value range 0-1,000,000)
        twapPriceFeed.downSlotsRatio = uint32(downSlotsRatio);

        return twapPriceFeed;
    }
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

**File:** target_chains/ethereum/sdk/solidity/PythStructs.sol (L43-52)
```text
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
