### Title
EVM TWAP Allows Same-Slot Start/End Points Causing Division-by-Zero Revert — (`target_chains/ethereum/contracts/contracts/pyth/Pyth.sol`)

### Summary

`validateTwapPriceInfo` in `Pyth.sol` uses a non-strict `>` comparison for `publishSlot`, permitting a caller to supply identical start and end VAA data (same slot). This causes `slotDiff = 0` in `calculateTwap`, triggering an integer division-by-zero panic revert. The Solana receiver correctly uses a strict `>` check that rejects equal slots; the EVM contract does not.

---

### Finding Description

`validateTwapPriceInfo` validates the relationship between the start and end `TwapPriceInfo` structs:

```solidity
// Pyth.sol line 604
if (twapPriceInfoStart.publishSlot > twapPriceInfoEnd.publishSlot) {
    revert PythErrors.InvalidTwapUpdateDataSet();
}
```

The guard only reverts when `startSlot > endSlot`. It silently accepts `startSlot == endSlot`. [1](#0-0) 

Immediately after validation, `calculateTwap` computes:

```solidity
uint64 slotDiff = twapPriceInfoEnd.publishSlot - twapPriceInfoStart.publishSlot; // = 0
int128 twapPrice = priceDiff / int128(uint128(slotDiff));   // division by zero → panic
uint128 twapConf  = confDiff / uint128(slotDiff);           // division by zero → panic
uint64 downSlotsRatio = (totalDownSlots * 1_000_000) / slotDiff; // division by zero → panic
``` [2](#0-1) 

By contrast, the Solana receiver enforces strict inequality:

```rust
require!(
    end_msg.publish_slot > start_msg.publish_slot,
    ReceiverError::InvalidTwapSlots
);
``` [3](#0-2) 

The analog to the PoolTogether bug is exact: PoolTogether used `currentPeriod == newestObservationPeriod` to decide whether to overwrite; Pyth EVM uses `startSlot > endSlot` (not `>=`) to decide whether to reject — both are off-by-one period/slot boundary checks that admit the degenerate same-period/same-slot case.

---

### Impact Explanation

Any caller of `parseTwapPriceFeedUpdates` who supplies identical bytes for `updateData[0]` and `updateData[1]` (or any two VAAs sharing the same `publishSlot` for the same feed) will receive a Solidity panic revert (error `0x12`). Because `parseTwapPriceFeedUpdates` is `payable`, the fee is refunded on revert, but:

1. **Protocol-level DoS**: Any on-chain protocol that calls `parseTwapPriceFeedUpdates` and allows user-supplied VAA bytes can be forced into a revert, blocking any logic that depends on the TWAP result.
2. **Griefing**: An attacker can cheaply and repeatedly cause the TWAP path to revert for any consumer that does not independently validate that `startSlot < endSlot` before calling. [4](#0-3) 

---

### Likelihood Explanation

The attack requires no privileged access and no fabricated data. A caller simply passes the same Wormhole-verified VAA bytes as both `updateData[0]` and `updateData[1]`. The `prevPublishTime < publishTime` uniqueness check passes for both (it is the same message), and the slot check passes because `startSlot == endSlot` is not rejected. This is a one-transaction, zero-cost (gas aside) trigger. [5](#0-4) 

---

### Recommendation

Change the slot comparison in `validateTwapPriceInfo` from `>` to `>=`, mirroring the Solana receiver:

```solidity
// Before (vulnerable)
if (twapPriceInfoStart.publishSlot > twapPriceInfoEnd.publishSlot) {

// After (fixed)
if (twapPriceInfoStart.publishSlot >= twapPriceInfoEnd.publishSlot) {
```

This ensures `slotDiff >= 1` before any division is attempted, eliminating the division-by-zero path and aligning EVM behaviour with the Solana implementation. [6](#0-5) 

---

### Proof of Concept

1. Deploy or interact with the live `Pyth.sol` contract on any EVM chain.
2. Obtain any valid Wormhole-signed TWAP VAA for a price feed (e.g., from Hermes).
3. Call `parseTwapPriceFeedUpdates` with `updateData = [vaa, vaa]` (same bytes twice) and the matching `priceIds`.
4. `validateTwapPriceInfo` passes: `startSlot == endSlot` is not rejected.
5. `calculateTwap` executes `priceDiff / int128(uint128(0))` → Solidity panic revert `0x12`.
6. The entire transaction reverts; any protocol logic depending on the TWAP return value is blocked. [7](#0-6)

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

**File:** target_chains/ethereum/contracts/contracts/pyth/Pyth.sol (L706-754)
```text
    /// @notice Calculates TWAP from two price points
    /// @dev The calculation is done by taking the difference of cumulative values and dividing by the time difference
    /// @param priceId The price feed ID
    /// @param twapPriceInfoStart The starting price point
    /// @param twapPriceInfoEnd The ending price point
    /// @return twapPriceFeed The calculated TWAP price feed
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

**File:** target_chains/solana/programs/pyth-solana-receiver/src/lib.rs (L539-543)
```rust
    // Validate slots
    require!(
        end_msg.publish_slot > start_msg.publish_slot,
        ReceiverError::InvalidTwapSlots
    );
```
