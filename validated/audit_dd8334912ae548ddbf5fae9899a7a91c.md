### Title
Incorrect `AVG_BLOCK_INTERVAL` Constant Causes Fee Estimator to Use Wrong Block-Count Targets — (`util/fee-estimator/src/constants.rs`)

---

### Summary

`AVG_BLOCK_INTERVAL` in the fee estimator is computed as the arithmetic mean of `MAX_BLOCK_INTERVAL` and `MIN_BLOCK_INTERVAL` (yielding 28 seconds), but the actual CKB target block time is approximately 14.4 seconds. Every derived target block count (`MAX_TARGET`, `LOW_TARGET`, `MEDIUM_TARGET`) is therefore roughly halved relative to its documented time window. Any RPC caller using `estimate_fee_rate` receives fee-rate estimates calibrated for approximately half the stated confirmation horizon, causing systematic fee overpayment.

---

### Finding Description

In `util/fee-estimator/src/constants.rs`:

```rust
/// Average block interval (28).
pub(crate) const AVG_BLOCK_INTERVAL: u64 = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;
//  = (48 + 8) / 2 = 28 seconds

/// Max target blocks, about 1 hour (128).
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
//  = 3600 / 28 = 128 blocks
```

`MAX_BLOCK_INTERVAL = 48` and `MIN_BLOCK_INTERVAL = 8` are the **allowed range bounds** for block production, not a meaningful average of actual block times. [1](#0-0) 

The actual CKB target block time is derived from the consensus parameters:

```
DEFAULT_EPOCH_DURATION_TARGET / GENESIS_EPOCH_LENGTH
= 14400 seconds / 1000 blocks
= 14.4 seconds per block
``` [2](#0-1) 

Using 28 seconds instead of ~14.4 seconds is an error of nearly 2×. All downstream constants inherit this error: [3](#0-2) 

| Constant | Blocks | Documented intent | Actual wall-clock time (at 14.4 s/block) |
|---|---|---|---|
| `DEFAULT_TARGET` | 128 | ~1 hour | ~30.7 minutes |
| `LOW_TARGET` | 64 | ~30 minutes | ~15.4 minutes |
| `MEDIUM_TARGET` | 42 | ~10 minutes | ~10.1 minutes |

`MEDIUM_TARGET` happens to be close to correct only because it is derived by dividing an already-halved `LOW_TARGET` by 3.

These constants are consumed directly by `FeeEstimator::target_blocks_for_estimate_mode`, which maps each `EstimateMode` to a block-count target and passes it to both the `ConfirmationFraction` and `WeightUnitsFlow` algorithms: [4](#0-3) 

---

### Impact Explanation

Any RPC caller invoking `estimate_fee_rate` with `NoPriority` or `LowPriority` receives a fee-rate recommendation calibrated for roughly half the stated time window. In practice this means:

- A wallet requesting a "1-hour" fee rate is given a rate sufficient to confirm in ~30 minutes — systematically overpaying by approximately 2×.
- A wallet requesting a "30-minute" fee rate is given a rate sufficient for ~15 minutes.

The `WeightUnitsFlow` algorithm also uses `MAX_TARGET` to bound the historical window it retains for flow-speed calculation, so the incorrect constant also skews the historical data window used for estimation: [5](#0-4) 

---

### Likelihood Explanation

The miscalculation is unconditional — it is baked into compile-time constants. Every call to `estimate_fee_rate` via the public RPC endpoint is affected. No special conditions or attacker interaction are required; any wallet or user relying on the fee estimator is impacted.

---

### Recommendation

Replace the arithmetic mean of the range bounds with the actual target block time derived from consensus parameters:

```rust
use ckb_chain_spec::consensus::{DEFAULT_EPOCH_DURATION_TARGET, GENESIS_EPOCH_LENGTH, TX_PROPOSAL_WINDOW};

/// Average block interval derived from epoch duration target (~14 seconds).
pub(crate) const AVG_BLOCK_INTERVAL: u64 = DEFAULT_EPOCH_DURATION_TARGET / GENESIS_EPOCH_LENGTH;

/// Max target blocks, about 1 hour (~257).
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
```

This aligns the fee estimator's time targets with the actual consensus-level block production rate.

---

### Proof of Concept

```python
# Current (incorrect)
MAX_BLOCK_INTERVAL = 48
MIN_BLOCK_INTERVAL = 8
AVG_BLOCK_INTERVAL = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) // 2  # = 28 s
MAX_TARGET = 3600 // AVG_BLOCK_INTERVAL                               # = 128 blocks
actual_time = MAX_TARGET * 14.4                                        # = 1843 s ≈ 30.7 min

# Correct
DEFAULT_EPOCH_DURATION_TARGET = 4 * 60 * 60   # 14400 s
GENESIS_EPOCH_LENGTH = 1000
avg_block_time = DEFAULT_EPOCH_DURATION_TARGET // GENESIS_EPOCH_LENGTH # = 14 s
correct_max_target = 3600 // avg_block_time                            # = 257 blocks
correct_time = correct_max_target * 14.4                               # = 3700 s ≈ 61.7 min

print(f"Current MAX_TARGET: {MAX_TARGET} blocks → {actual_time/60:.1f} min (expected 60 min)")
print(f"Correct MAX_TARGET: {correct_max_target} blocks → {correct_time/60:.1f} min")
# Current MAX_TARGET: 128 blocks → 30.7 min (expected 60 min)
# Correct MAX_TARGET: 257 blocks → 61.7 min
```

### Citations

**File:** spec/src/consensus.rs (L59-80)
```rust
pub(crate) const GENESIS_EPOCH_LENGTH: u64 = 1_000;

// o_ideal = 1/40 = 2.5%
pub(crate) const DEFAULT_ORPHAN_RATE_TARGET: (u32, u32) = (1, 40);

/// max block interval, 48 seconds
pub const MAX_BLOCK_INTERVAL: u64 = 48;
/// min block interval, 8 seconds
pub const MIN_BLOCK_INTERVAL: u64 = 8;

/// cycles of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_CYCLES: Cycle = 3_500_000;
/// bytes of a typical two-in-two-out tx.
pub const TWO_IN_TWO_OUT_BYTES: u64 = 597;
/// count of two-in-two-out txs a block should capable to package.
const TWO_IN_TWO_OUT_COUNT: u64 = 1_000;
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 4 hours, unit: second
const MILLISECONDS_IN_A_SECOND: u64 = 1000;
const MAX_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MIN_BLOCK_INTERVAL; // 1800
const MIN_EPOCH_LENGTH: u64 = DEFAULT_EPOCH_DURATION_TARGET / MAX_BLOCK_INTERVAL; // 300
pub(crate) const DEFAULT_PRIMARY_EPOCH_REWARD_HALVING_INTERVAL: EpochNumber =
    4 * 365 * 24 * 60 * 60 / DEFAULT_EPOCH_DURATION_TARGET; // every 4 years
```

**File:** util/fee-estimator/src/constants.rs (L1-25)
```rust
//! The constants for the fee estimator.

use ckb_chain_spec::consensus::{MAX_BLOCK_INTERVAL, MIN_BLOCK_INTERVAL, TX_PROPOSAL_WINDOW};
use ckb_types::core::{BlockNumber, FeeRate};

/// Average block interval (28).
pub(crate) const AVG_BLOCK_INTERVAL: u64 = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;

/// Max target blocks, about 1 hour (128).
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;
/// Min target blocks, in next block (5).
/// NOTE After tests, 3 blocks are too strict; so to adjust larger: 5.
pub(crate) const MIN_TARGET: BlockNumber = (TX_PROPOSAL_WINDOW.closest() + 1) + 2;

/// Lowest fee rate.
pub(crate) const LOWEST_FEE_RATE: FeeRate = FeeRate::from_u64(1000);

/// Target blocks for no priority (lowest priority, about 1 hour, 128).
pub const DEFAULT_TARGET: BlockNumber = MAX_TARGET;
/// Target blocks for low priority (about 30 minutes, 64).
pub const LOW_TARGET: BlockNumber = DEFAULT_TARGET / 2;
/// Target blocks for medium priority (about 10 minutes, 42).
pub const MEDIUM_TARGET: BlockNumber = LOW_TARGET / 3;
/// Target blocks for high priority (3).
pub const HIGH_TARGET: BlockNumber = MIN_TARGET;
```

**File:** util/fee-estimator/src/estimator/mod.rs (L41-48)
```rust
    pub const fn target_blocks_for_estimate_mode(estimate_mode: EstimateMode) -> BlockNumber {
        match estimate_mode {
            EstimateMode::NoPriority => constants::DEFAULT_TARGET,
            EstimateMode::LowPriority => constants::LOW_TARGET,
            EstimateMode::MediumPriority => constants::MEDIUM_TARGET,
            EstimateMode::HighPriority => constants::HIGH_TARGET,
        }
    }
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L147-151)
```rust
    fn expire(&mut self) {
        let historical_blocks = Self::historical_blocks(constants::MAX_TARGET);
        let expired_tip = self.current_tip.saturating_sub(historical_blocks);
        self.txs.retain(|&num, _| num >= expired_tip);
    }
```
