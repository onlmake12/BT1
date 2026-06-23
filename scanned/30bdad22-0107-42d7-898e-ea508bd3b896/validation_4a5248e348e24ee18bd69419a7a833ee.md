Looking at the code carefully, I need to trace the exact bounds of both vectors and the guard logic.

### Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` access with a `tx_age >= len` check but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a corresponding bounds check. When a tracked tx ages beyond `MAX_CONFIRM_BLOCKS` (1000) blocks and is then evicted from the tx-pool, `tx_age - 1 >= 1000` produces an index-out-of-bounds Rust panic, crashing the node.

---

### Finding Description

Both `block_unconfirmed_txs` and `confirm_blocks_to_failed_txs` are allocated with the same length `max_confirm_blocks = 1000`: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 routes `block_unconfirmed_txs` access to the `old_unconfirmed_txs` counter when `tx_age >= 1000`, correctly avoiding an OOB there: [2](#0-1) 

However, the `confirm_blocks_to_failed_txs` write at line 215 is **not** guarded by any equivalent check: [3](#0-2) 

- When `tx_age = 1000`: `tx_age - 1 = 999` → valid (last index).
- When `tx_age = 1001`: `tx_age - 1 = 1000` → **out of bounds** on a 1000-element `Vec` → Rust panic.

The panic is triggered via the call chain:

`reject_tx` → `drop_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx` [4](#0-3) [5](#0-4) 

The public `reject_tx` on `FeeEstimator` delegates directly to the `ConfirmationFraction` algorithm: [6](#0-5) 

---

### Impact Explanation

A Rust index-out-of-bounds on a `Vec` is an unrecoverable panic. Unless the caller wraps the call in `std::panic::catch_unwind` (it does not), the thread aborts and the node process crashes. This is a remote-triggerable node crash (DoS).

---

### Likelihood Explanation

The `TxPool` has a wall-clock expiry (`expiry_hours`) and a size-based eviction policy: [7](#0-6) 

With a default expiry of many hours and ~10-second block times, a tx submitted at tip height H can easily remain in the pool for 1001+ blocks before being evicted. An unprivileged attacker only needs to:

1. Submit a valid low-fee-rate tx (accepted into the pool and tracked by the fee estimator via `accept_tx`).
2. Wait for 1001+ blocks to pass without the tx being mined.
3. Trigger eviction (e.g., flood the pool with higher-fee txs to push the low-fee tx out, or wait for the wall-clock expiry).

Step 3 causes `reject_tx` → `remove_unconfirmed_tx` with `count_failure=true` and `tx_age ≥ 1001`, producing the panic.

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Or use `.get_mut(tx_age - 1)` and silently skip if out of range, consistent with the design intent that txs older than `MAX_CONFIRM_BLOCKS` are simply not tracked for failure statistics.

---

### Proof of Concept

```rust
#[test]
fn test_oob_panic_on_old_tx_eviction() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Simulate 1001 blocks passing: move_track_window for each block
    for h in 1u64..=1001 {
        stat.move_track_window(h);
    }

    // tx_age = 1001 - 0 = 1001; confirm_blocks_to_failed_txs[1000] → PANIC
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

Running this test panics with `index out of bounds: the len is 1000 but the index is 1000`.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-213)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L214-216)
```rust
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L83-89)
```rust
    /// Rejects a tx.
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
    }
```

**File:** tx-pool/src/pool.rs (L46-57)
```rust
    pub(crate) expiry: u64,
    // conflicted transaction cache
    pub(crate) conflicts_cache: lru::LruCache<ProposalShortId, TransactionView>,
    // conflicted transaction outputs cache, input -> tx_short_id
    pub(crate) conflicts_outputs_cache: lru::LruCache<OutPoint, ProposalShortId>,
}

impl TxPool {
    /// Create new TxPool
    pub fn new(config: TxPoolConfig, snapshot: Arc<Snapshot>) -> TxPool {
        let recent_reject = Self::build_recent_reject(&config);
        let expiry = config.expiry_hours as u64 * 60 * 60 * 1000;
```
