### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` via Long-Lived Unconfirmed Transaction Eviction — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

When the `ConfirmationFraction` fee estimator is active, an unprivileged attacker can crash the CKB node by submitting a valid transaction that remains unconfirmed for more than `MAX_CONFIRM_BLOCKS` (1000) blocks and is then evicted from the tx pool. The eviction path calls `remove_unconfirmed_tx` with `tx_age > 1000`, which performs an unchecked index `confirm_blocks_to_failed_txs[tx_age - 1]` on a Vec of exactly 1000 elements, causing an out-of-bounds panic.

---

### Finding Description

`confirm_blocks_to_failed_txs` is initialized with exactly `max_confirm_blocks = 1000` entries (indices `0..=999`): [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 only protects the `block_unconfirmed_txs` access. The subsequent `count_failure` branch at line 215 uses `tx_age - 1` as a direct, unchecked index into `confirm_blocks_to_failed_txs`: [2](#0-1) 

When `tx_age = 1001`, the guard at line 208 (`tx_age >= 1000`) is satisfied and correctly routes the unconfirmed-tracking decrement to `old_unconfirmed_txs`. However, execution falls through to line 215 where `confirm_blocks_to_failed_txs[1000]` is accessed — index 1000 on a 1000-element Vec — causing a Rust index-out-of-bounds **panic** (process abort).

---

### Impact Explanation

Rust's `Vec` indexing always panics on out-of-bounds access in both debug and release builds. A panic in the fee estimator propagates up through the tx-pool service thread, crashing the node process. This is a **remote, unprivileged node crash**.

---

### Likelihood Explanation

The `ConfirmationFraction` estimator is a real production variant instantiated via `new_confirmation_fraction()`: [3](#0-2) 

The `reject_tx` dispatch is wired directly to the algorithm: [4](#0-3) 

The `drop_tx_inner` call with `count_failure=true` is the path taken for all non-commit removals (expiry, pool size limit eviction): [5](#0-4) 

A transaction with a low fee rate that is never mined will naturally age past 1000 blocks before the pool's time-based expiry (default 12 hours ≈ ~1440 blocks at 10s/block) evicts it. No special privileges are required — only a valid transaction submission via RPC.

---

### Recommendation

Add a bounds guard before the `confirm_blocks_to_failed_txs` index access:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Or clamp `tx_age` to `self.confirm_blocks_to_failed_txs.len()` before use, consistent with how `block_unconfirmed_txs` is already guarded.

---

### Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    // Add a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate 1001 blocks passing: tx_age = 1001 - 0 = 1001
    // confirm_blocks_to_failed_txs has len=1000, so index 1000 is OOB
    // This panics:
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

`tx_age = 1001`, `tx_age - 1 = 1000`, Vec length = 1000 → **index out of bounds: the len is 1000 but the index is 1000** → node crash. [6](#0-5)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L197-217)
```rust
    fn remove_unconfirmed_tx(
        &mut self,
        entry_height: u64,
        tip_height: u64,
        bucket_index: usize,
        count_failure: bool,
    ) {
        let tx_age = tip_height.saturating_sub(entry_height) as usize;
        if tx_age < 1 {
            return;
        }
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
        }
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L416-425)
```rust
    fn drop_tx_inner(&mut self, tx_hash: &Byte32, count_failure: bool) -> Option<TxRecord> {
        self.tracked_txs.remove(tx_hash).inspect(|tx_record| {
            self.tx_confirm_stat.remove_unconfirmed_tx(
                tx_record.height,
                self.best_height,
                tx_record.bucket_index,
                count_failure,
            );
        })
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L35-38)
```rust
    pub fn new_confirmation_fraction() -> Self {
        let algo = confirmation_fraction::Algorithm::new();
        FeeEstimator::ConfirmationFraction(Arc::new(RwLock::new(algo)))
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L84-89)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }
```
