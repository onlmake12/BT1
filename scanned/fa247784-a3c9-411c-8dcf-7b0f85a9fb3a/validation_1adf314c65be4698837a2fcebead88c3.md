### Title
Wrong Block Time Assumption in Fee Estimator Causes Miscalibrated Priority Targets — (`util/fee-estimator/src/constants.rs`)

---

### Summary

`util/fee-estimator/src/constants.rs` computes `AVG_BLOCK_INTERVAL` as the arithmetic mean of `MAX_BLOCK_INTERVAL` (48 s) and `MIN_BLOCK_INTERVAL` (8 s), yielding 28 s. This value is then used to derive all fee-priority block-count targets. However, CKB's actual average block time is governed by `DEFAULT_EPOCH_DURATION_TARGET` (4 hours = 14 400 s) divided by the epoch length. At the genesis epoch length of 1 000 blocks the average block time is **14.4 s**, not 28 s — a factor-of-~2 discrepancy. Every documented time label attached to a priority target is therefore roughly twice as long as the real elapsed time those blocks represent.

---

### Finding Description

`AVG_BLOCK_INTERVAL` is defined as:

```rust
// util/fee-estimator/src/constants.rs
pub(crate) const AVG_BLOCK_INTERVAL: u64 = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;
// = (48 + 8) / 2 = 28 seconds
``` [1](#0-0) 

`MAX_BLOCK_INTERVAL` and `MIN_BLOCK_INTERVAL` are the **bounding** values of the dynamic difficulty adjustment, not the expected steady-state block time:

```rust
// spec/src/consensus.rs
pub const MAX_BLOCK_INTERVAL: u64 = 48;   // seconds
pub const MIN_BLOCK_INTERVAL: u64 = 8;    // seconds
pub(crate) const DEFAULT_EPOCH_DURATION_TARGET: u64 = 4 * 60 * 60; // 14 400 s
``` [2](#0-1) 

The epoch-length bounds derived from these values are:

```
MAX_EPOCH_LENGTH = 14400 / 8  = 1800 blocks  → block time = 8 s
MIN_EPOCH_LENGTH = 14400 / 48 = 300  blocks  → block time = 48 s
GENESIS_EPOCH_LENGTH = 1000 blocks            → block time = 14.4 s
``` [3](#0-2) 

The arithmetic mean of the two extremes (28 s) is not the expected average block time; the expected average is `DEFAULT_EPOCH_DURATION_TARGET / typical_epoch_length ≈ 14.4 s`. All priority targets are derived from the wrong base:

```rust
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL; // 128 blocks
pub const DEFAULT_TARGET: BlockNumber = MAX_TARGET;          // "about 1 hour"
pub const LOW_TARGET:     BlockNumber = DEFAULT_TARGET / 2;  // "about 30 minutes"
pub const MEDIUM_TARGET:  BlockNumber = LOW_TARGET / 3;      // "about 10 minutes"
``` [4](#0-3) 

Actual elapsed time at 14.4 s/block:

| Priority label | Block count | Documented | Actual elapsed |
|---|---|---|---|
| `NoPriority` / `DEFAULT_TARGET` | 128 | ~1 hour | ~30 minutes |
| `LowPriority` / `LOW_TARGET` | 64 | ~30 minutes | ~15 minutes |
| `MediumPriority` / `MEDIUM_TARGET` | 21 | ~10 minutes | ~5 minutes |

These constants are consumed directly by the fee estimator dispatcher:

```rust
// util/fee-estimator/src/estimator/mod.rs
pub const fn target_blocks_for_estimate_mode(estimate_mode: EstimateMode) -> BlockNumber {
    match estimate_mode {
        EstimateMode::NoPriority   => constants::DEFAULT_TARGET,
        EstimateMode::LowPriority  => constants::LOW_TARGET,
        EstimateMode::MediumPriority => constants::MEDIUM_TARGET,
        EstimateMode::HighPriority => constants::HIGH_TARGET,
    }
}
``` [5](#0-4) 

---

### Impact Explanation

The fee estimator's look-ahead window is approximately half the documented duration. Because the algorithm sizes its window in blocks, not wall-clock time, every priority tier targets a confirmation horizon that is ~2× shorter than its label claims. Concretely:

- A caller requesting `NoPriority` ("~1 hour") receives a fee rate calibrated for ~30 minutes of block production. This systematically **over-estimates** the required fee rate for low-urgency transactions, causing users to overpay.
- Conversely, if a user manually interprets the block-count target as a wall-clock duration and adjusts their own fee logic accordingly, they will **under-estimate** the required fee and risk mempool stagnation.

The `estimate_fee_rate` RPC is reachable by any unprivileged RPC caller.

---

### Likelihood Explanation

This is a constant-level miscalibration present in every build. Any node operator who enables the fee estimator and any RPC client calling `estimate_fee_rate` is affected unconditionally. No special network conditions or attacker actions are required; the wrong assumption is baked in at compile time.

---

### Recommendation

Replace the arithmetic-mean heuristic with the protocol-defined expected block time:

```rust
// Derived from the epoch duration target and genesis epoch length,
// which is the protocol's own definition of expected block time.
pub(crate) const AVG_BLOCK_INTERVAL: u64 =
    DEFAULT_EPOCH_DURATION_TARGET / GENESIS_EPOCH_LENGTH; // 14400 / 1000 = 14 s
```

Alternatively, use a weighted or harmonic mean that accounts for the epoch-length distribution, or expose `AVG_BLOCK_INTERVAL` as a configurable parameter so operators can tune it to observed network conditions. All derived targets (`MAX_TARGET`, `LOW_TARGET`, `MEDIUM_TARGET`) will then automatically reflect correct wall-clock durations.

---

### Proof of Concept

1. `MIN_BLOCK_INTERVAL = 8`, `MAX_BLOCK_INTERVAL = 48` → `AVG_BLOCK_INTERVAL = 28`. [1](#0-0) 

2. `DEFAULT_EPOCH_DURATION_TARGET = 14400 s`, `GENESIS_EPOCH_LENGTH = 1000` → expected block time = **14.4 s**. [6](#0-5) 

3. `MAX_TARGET = 3600 / 28 = 128 blocks`. At 14.4 s/block: `128 × 14.4 = 1843 s ≈ 30 min`, not 1 hour. [7](#0-6) 

4. The estimator dispatcher passes these block counts directly to the algorithm, so the miscalibration propagates to every `estimate_fee_rate` RPC response. [5](#0-4)

### Citations

**File:** util/fee-estimator/src/constants.rs (L6-7)
```rust
/// Average block interval (28).
pub(crate) const AVG_BLOCK_INTERVAL: u64 = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;
```

**File:** util/fee-estimator/src/constants.rs (L9-23)
```rust
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
```

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
