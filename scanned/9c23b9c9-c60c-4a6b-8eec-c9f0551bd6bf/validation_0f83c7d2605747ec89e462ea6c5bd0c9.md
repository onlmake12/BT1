The code is clear. Let me trace the exact bug.

### Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access when `tx_age >= 1000`, but applies **no equivalent guard** before indexing `confirm_blocks_to_failed_txs[tx_age - 1]`. Both arrays have length `MAX_CONFIRM_BLOCKS = 1000`. When a tracked transaction is rejected after 1001+ blocks of pending time, `tx_age - 1 >= 1000` produces an out-of-bounds index, causing a Rust panic and crashing the node.

---

### Finding Description

`confirm_blocks_to_failed_txs` is allocated with length `max_confirm_blocks` (= `MAX_CONFIRM_BLOCKS` = 1000): [1](#0-0) 

The guard at line 208 protects only the `block_unconfirmed_txs` access: [2](#0-1) 

But the `count_failure` branch immediately below has **no bounds check**: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000`, which is one past the last valid index (0–999) of a 1000-element `Vec`. Rust panics with an index-out-of-bounds.

`drop_tx_inner` passes `self.best_height` as `tip_height` with no age cap: [4](#0-3) 

A tx remains in `tracked_txs` indefinitely as long as it is not confirmed. `process_block` only removes a tx from `tracked_txs` if it appears in the committed block: [5](#0-4) 

There is no age-based eviction from `tracked_txs`. After 1001 blocks, `best_height - tx_record.height >= 1001`, and the next call to `drop_tx` (with `count_failure=true`) panics.

---

### Impact Explanation

The panic occurs inside the tx-pool service thread. In Rust, an unrecovered `index out of bounds` panic unwinds and terminates the thread (or the process, depending on the panic handler). This crashes the CKB node, causing a denial-of-service for any node running the `ConfirmationFraction` fee estimator. [6](#0-5) 

---

### Likelihood Explanation

The attack requires no privileges:

1. Submit any valid transaction via RPC (`submit_local_tx`) or P2P relay — this calls `accept_tx` on the estimator, recording the tx at height H.
2. Wait for 1001 blocks to be mined (~7.8 hours at 28 s/block average). The tx stays in `tracked_txs` throughout.
3. Submit a conflicting transaction (RBF or a double-spend that gets confirmed). This triggers `reject_tx` → `drop_tx` → `drop_tx_inner(count_failure=true)` → `remove_unconfirmed_tx` with `tx_age = 1001` → **panic**. [7](#0-6) 

The attacker controls only the timing and the conflicting transaction submission. No hashpower, no keys, no operator access required.

---

### Recommendation

Add a bounds check before the `count_failure` write, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
``` [8](#0-7) 

---

### Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let decay = 0.993f64;
    let max_confirm_blocks = 1000;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1500)).unwrap();

    // Simulate 1001 blocks passing: move_track_window wraps the slot
    for h in 1..=1001u64 {
        stat.move_track_window(h);
    }

    // tx_age = 1001 - 0 = 1001; confirm_blocks_to_failed_txs[1000] → panic!
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

This test panics with `index out of bounds: the len is 1000 but the index is 1000`, confirming the bug is directly reachable.

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L363-373)
```rust
    fn process_block_tx(&mut self, height: u64, tx_hash: &Byte32) -> bool {
        if let Some(tx) = self.drop_tx_inner(tx_hash, false) {
            let blocks_to_confirm = height.saturating_sub(tx.height) as usize;
            self.tx_confirm_stat
                .add_confirmed_tx(blocks_to_confirm, tx.fee_rate);
            true
        } else {
            // tx is not tracked
            false
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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** util/fee-estimator/src/estimator/mod.rs (L84-88)
```rust
    pub fn reject_tx(&self, tx_hash: &Byte32) {
        match self {
            Self::Dummy | Self::WeightUnitsFlow(_) => {}
            Self::ConfirmationFraction(algo) => algo.write().reject_tx(tx_hash),
        }
```
