### Title
Hardcoded `AVG_BLOCK_INTERVAL` Assumption Causes Miscalibrated Fee-Rate Estimates - (File: `util/fee-estimator/src/constants.rs`)

---

### Summary

The fee estimator converts wall-clock time targets ("~1 hour", "~30 minutes", "~10 minutes") into block-count targets using a compile-time constant `AVG_BLOCK_INTERVAL = 28 s`, derived as the arithmetic mean of `MIN_BLOCK_INTERVAL` (8 s) and `MAX_BLOCK_INTERVAL` (48 s). CKB's actual block interval is dynamic and can deviate substantially from this assumed average, causing all priority-tier block targets to be systematically wrong. Any RPC caller relying on `estimate_fee_rate` receives miscalibrated recommendations.

---

### Finding Description

`AVG_BLOCK_INTERVAL` is a compile-time constant:

```rust
pub(crate) const AVG_BLOCK_INTERVAL: u64 = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2; // = 28
pub(crate) const MAX_TARGET: BlockNumber = (60 * 60) / AVG_BLOCK_INTERVAL;               // = 128 blocks
pub const LOW_TARGET: BlockNumber    = DEFAULT_TARGET / 2;   // 64 blocks  (~30 min)
pub const MEDIUM_TARGET: BlockNumber = LOW_TARGET / 3;       // 42 blocks  (~10 min)
``` [1](#0-0) 

These block-count targets are then used directly by both fee-estimation algorithms (`ConfirmationFraction` and `WeightUnitsFlow`) to decide how many historical blocks to sample and how many future blocks to simulate:

```rust
pub fn target_blocks_for_estimate_mode(estimate_mode: EstimateMode) -> BlockNumber {
    match estimate_mode {
        EstimateMode::NoPriority   => constants::DEFAULT_TARGET,  // 128
        EstimateMode::LowPriority  => constants::LOW_TARGET,      // 64
        EstimateMode::MediumPriority => constants::MEDIUM_TARGET, // 42
        EstimateMode::HighPriority => constants::HIGH_TARGET,     // 3
    }
}
``` [2](#0-1) 

The same pattern appears in the sync subsystem, where `tip_synced()` computes a re-sync pause using the same hardcoded arithmetic mean:

```rust
fn tip_synced(&mut self) {
    let now = unix_time_as_millis();
    let avg_interval = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;
    self.headers_sync_state = HeadersSyncState::TipSynced(now + avg_interval * 1000);
}
``` [3](#0-2) 

CKB's consensus uses a dynamic difficulty adjustment mechanism. The actual block interval is not fixed at 28 s; it is bounded by `[MIN_BLOCK_INTERVAL, MAX_BLOCK_INTERVAL]` = `[8 s, 48 s]` and adjusts epoch-by-epoch based on observed orphan rate and hash rate. [4](#0-3) 

---

### Impact Explanation

When the actual network block interval diverges from the assumed 28 s:

- **Actual interval < 28 s** (e.g., 10 s during high hash-rate periods): `MAX_TARGET = 128 blocks` represents only ~21 minutes, not 1 hour. A user submitting a "no-priority" transaction expecting ~1 hour confirmation will overpay fees.
- **Actual interval > 28 s** (e.g., 40 s during low hash-rate periods): `MAX_TARGET = 128 blocks` represents ~85 minutes. A user submitting a "medium-priority" transaction (42 blocks, assumed ~10 min) may wait ~28 minutes. More critically, the `WeightUnitsFlow` algorithm's `do_estimate` uses `target_blocks` to compute `removed_weight = (MAX_BLOCK_BYTES * 85/100) * target_blocks`, so a miscalibrated target directly skews the estimated clearance of the mempool, potentially recommending a fee rate too low to confirm within the user's intended time window — leaving the transaction stuck in the mempool. [5](#0-4) 

---

### Likelihood Explanation

CKB's dynamic difficulty adjustment is a core protocol feature, not an edge case. The actual average block interval over any given epoch can be anywhere in `[8, 48]` seconds. The arithmetic mean of the bounds (28 s) is not a guaranteed or even typical value. Any sustained deviation — which is normal during hash-rate fluctuations — causes the fee estimator to produce systematically wrong block-count targets. The `estimate_fee_rate` RPC is publicly accessible to any node operator or application developer.

---

### Recommendation

Replace the compile-time constant `AVG_BLOCK_INTERVAL` with a runtime value derived from the actual observed block interval (e.g., from the current epoch's `epoch_duration / epoch_length`). The `Consensus` struct already exposes `epoch_duration_target()` and the current epoch length is available from the chain tip, making a runtime calculation straightforward. At minimum, document that all priority-tier block targets are approximations calibrated to a 28 s assumed interval and may be inaccurate.

---

### Proof of Concept

1. Observe a CKB epoch where hash rate is high and actual block interval averages ~10 s.
2. Call `estimate_fee_rate` with `EstimateMode::MediumPriority` (target = 42 blocks, assumed ~10 min).
3. At 10 s/block, 42 blocks = ~7 minutes — close enough. But call with `EstimateMode::NoPriority` (128 blocks, assumed ~1 hour): at 10 s/block, 128 blocks = ~21 minutes. The returned fee rate is calibrated for 128 blocks of mempool clearance, but the user's transaction confirms in ~21 minutes, meaning they overpaid.
4. Conversely, in a low hash-rate epoch (~40 s/block): `MEDIUM_TARGET = 42 blocks` = ~28 minutes. The `WeightUnitsFlow` algorithm computes `removed_weight` for only 42 blocks of mining, underestimating how much backlog will clear, and may recommend a fee rate insufficient to confirm within the user's intended ~10-minute window — leaving the transaction unconfirmed. [6](#0-5) [5](#0-4)

### Citations

**File:** util/fee-estimator/src/constants.rs (L6-25)
```rust
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

**File:** sync/src/types/mod.rs (L101-105)
```rust
    fn tip_synced(&mut self) {
        let now = unix_time_as_millis();
        let avg_interval = (MAX_BLOCK_INTERVAL + MIN_BLOCK_INTERVAL) / 2;
        self.headers_sync_state = HeadersSyncState::TipSynced(now + avg_interval * 1000);
    }
```

**File:** spec/src/consensus.rs (L64-78)
```rust
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
```

**File:** util/fee-estimator/src/estimator/weight_units_flow.rs (L279-298)
```rust
        for bucket_index in 1..=max_bucket_index {
            let current_weight = current_weight_buckets[bucket_index];
            let added_weight = flow_speed_buckets[bucket_index] * target_blocks;
            // Note: blocks are not full even there are many pending transactions,
            // since `MAX_BLOCK_PROPOSALS_LIMIT = 1500`.
            let removed_weight = (MAX_BLOCK_BYTES * 85 / 100) * target_blocks;
            let passed = current_weight + added_weight <= removed_weight;
            ckb_logger::trace!(
                ">>> bucket[{}]: {}; {} + {} - {}",
                bucket_index,
                passed,
                current_weight,
                added_weight,
                removed_weight
            );
            if passed {
                let fee_rate = Self::lowest_fee_rate_by_bucket_index(bucket_index);
                return Ok(fee_rate);
            }
        }
```
