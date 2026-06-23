### Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`TxConfirmStat::remove_unconfirmed_tx` correctly guards the `block_unconfirmed_txs` circular-buffer access when `tx_age >= 1000`, but the immediately following `confirm_blocks_to_failed_txs[tx_age - 1]` index is **outside that guard** and has no bounds check. When a tracked transaction is evicted after more than 1000 blocks, `tx_age - 1 >= 1000` indexes past the end of a `Vec` of length 1000, producing an unconditional Rust index-out-of-bounds panic.

---

### Finding Description

`confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are both initialized with `MAX_CONFIRM_BLOCKS = 1000` rows: [1](#0-0) 

The guard at line 208 protects only the `block_unconfirmed_txs` write: [2](#0-1) 

But the `count_failure` branch at line 214–216 is **unconditional** — it executes regardless of `tx_age`: [3](#0-2) 

When `tx_age > 1000`, `tx_age - 1 >= 1000` is an out-of-bounds index into a `Vec` of length 1000. Rust panics here with no recovery.

`count_failure = true` is set only by the eviction path: [4](#0-3) 

which is called from `reject_tx`: [5](#0-4) 

Confirmed txs use `count_failure = false` (via `process_block_tx`) and are safe.

---

### Impact Explanation

A panic in the fee estimator propagates to the tx-pool service thread. If the `ConfirmationFraction` estimator is active, the panic is unrecoverable and crashes the tx-pool service, halting transaction acceptance and block template generation on the node.

---

### Likelihood Explanation

CKB blocks are produced every ~8–10 seconds. 1001 blocks ≈ 2.2–2.8 hours. The CKB tx-pool default expiry is on the order of days, so a low-fee-rate transaction submitted by any unprivileged user can trivially remain unconfirmed for 1001+ blocks before being evicted. No special privilege, key, or hashpower is required — only a valid transaction submission followed by waiting.

The bug is conditional on the node operator having selected the `ConfirmationFraction` algorithm (not `Dummy` or `WeightUnitsFlow`): [6](#0-5) 

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard:

```rust
if count_failure && tx_age >= 1 && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This clamps the failure recording to the tracked window and silently drops samples for txs older than `MAX_CONFIRM_BLOCKS`, which is already the intended behavior for the unconfirmed-tx tracking side.

---

### Proof of Concept

```rust
#[test]
fn test_no_panic_on_long_lived_tx() {
    let mut stat = TxConfirmStat::default(); // MAX_CONFIRM_BLOCKS = 1000
    let entry_height: u64 = 0;
    let fee_rate = FeeRate::from_u64(1000);
    let bucket_index = stat.add_unconfirmed_tx(entry_height, fee_rate).unwrap();

    // Simulate 1001 blocks passing
    let tip_height: u64 = 1001;
    // tx_age = 1001, tx_age - 1 = 1000, confirm_blocks_to_failed_txs.len() = 1000
    // → index 1000 on a Vec of length 1000 → panic!
    stat.remove_unconfirmed_tx(entry_height, tip_height, bucket_index, true);
}
```

This test panics on the current code at: [7](#0-6)

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L428-430)
```rust
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L19-26)
```rust
pub enum FeeEstimator {
    /// Dummy fee estimate algorithm; just do nothing.
    Dummy,
    /// Confirmation fraction fee estimator algorithm.
    ConfirmationFraction(Arc<RwLock<confirmation_fraction::Algorithm>>),
    /// Weight-Units flow fee estimator algorithm.
    WeightUnitsFlow(Arc<RwLock<weight_units_flow::Algorithm>>),
}
```
