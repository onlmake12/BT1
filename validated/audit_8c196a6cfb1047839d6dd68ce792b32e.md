### Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`TxConfirmStat::remove_unconfirmed_tx` unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without bounding `tx_age` against the array's length. When a tracked transaction is rejected from the tx pool after more than `MAX_CONFIRM_BLOCKS` (1000) blocks, `tx_age - 1 >= 1000` and Rust's bounds-checked indexing panics, crashing the node.

---

### Finding Description

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices 0–999): [1](#0-0) 

The guard at line 208 only decides *which counter* to decrement for the unconfirmed-tx bookkeeping — it does **not** bound `tx_age` before the `confirm_blocks_to_failed_txs` write: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is past the end of the 1000-element vector. Rust's indexing operator always panics on out-of-bounds access (in both debug and release builds), so this is an unconditional node crash.

The `count_failure = true` path is taken exclusively through `drop_tx` → `reject_tx`: [3](#0-2) [4](#0-3) 

`drop_tx_inner` passes `self.best_height` as `tip_height`: [5](#0-4) 

A tx is tracked with `entry_height = self.best_height` at the time of `accept_tx`: [6](#0-5) 

There is no cap on how long a tracked tx can remain in `tracked_txs`. If `best_height` advances by more than 1000 blocks before `reject_tx` is called, `tx_age > 1000` and the panic fires.

---

### Impact Explanation

Any CKB node running the `confirmation_fraction` fee estimator crashes (panic/abort) the moment a tracked transaction is evicted from the tx pool after sitting there for more than 1000 blocks. This is a **node crash** — a complete loss of availability for the affected node. Because tx pool eviction is a normal operational event (triggered by pool-size limits, RBF replacement, or explicit removal), the crash is not hypothetical.

---

### Likelihood Explanation

The trigger requires only two ordinary events:
1. A transaction is accepted into the tx pool and tracked by the fee estimator.
2. That transaction is later evicted (not confirmed) after 1001+ blocks have elapsed.

An attacker can deliberately engineer this: submit a minimum-fee transaction, wait 1001 blocks, then flood the pool with higher-fee transactions to force eviction of the original. No special privilege, no PoW, no key material is required — only the ability to submit transactions via RPC or P2P relay.

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write in `remove_unconfirmed_tx`:

```rust
if count_failure {
    let fail_index = tx_age - 1;
    if fail_index < self.confirm_blocks_to_failed_txs.len() {
        self.confirm_blocks_to_failed_txs[fail_index][bucket_index] += 1f64;
    }
}
```

This mirrors the existing guard for `block_unconfirmed_txs` and silently saturates the failure counter at the maximum tracked age, which is the correct semantic (the tx aged out of the tracking window).

---

### Proof of Concept

```rust
#[test]
fn test_oob_panic_tx_age_gt_max_confirm_blocks() {
    use super::*;
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000; // MAX_CONFIRM_BLOCKS
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    let entry_height: u64 = 0;
    let fee_rate = FeeRate::from_u64(1500);

    // Track the tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(entry_height, fee_rate).unwrap();

    // Simulate 1001 blocks passing: move_track_window is called for each block.
    // After 1000 calls the slot for height 0 is zeroed and moved to old_unconfirmed_txs.
    for h in 1..=1001u64 {
        stat.move_track_window(h);
    }

    // tip_height - entry_height = 1001 > 1000 = MAX_CONFIRM_BLOCKS
    // count_failure = true  →  confirm_blocks_to_failed_txs[1000] → PANIC (OOB)
    stat.remove_unconfirmed_tx(entry_height, 1001, bucket_index, true);
}
```

Running this test (even in release mode) produces:
```
thread 'test_oob_panic_tx_age_gt_max_confirm_blocks' panicked at
'index out of bounds: the len is 1000 but the index is 1000'
util/fee-estimator/src/estimator/confirmation_fraction.rs:215
```

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L140-141)
```rust
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-215)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L469-473)
```rust
    pub fn accept_tx(&mut self, tx_hash: Byte32, info: TxEntryInfo) {
        let weight = get_transaction_weight(info.size as usize, info.cycles);
        let fee_rate = FeeRate::calculate(info.fee, weight);
        self.track_tx(tx_hash, fee_rate, self.current_tip)
    }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L475-477)
```rust
    pub fn reject_tx(&mut self, tx_hash: &Byte32) {
        let _ = self.drop_tx(tx_hash);
    }
```
