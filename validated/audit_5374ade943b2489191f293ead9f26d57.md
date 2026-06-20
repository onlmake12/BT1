### Title
Missing Bounds Check on `confirm_blocks_to_failed_txs` in `TxConfirmStat::remove_unconfirmed_tx` Causes Node Panic — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access when `tx_age >= MAX_CONFIRM_BLOCKS` (1000), but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a corresponding bounds check. When a tracked transaction ages beyond 1000 blocks and is then rejected, `tx_age - 1 >= 1000` produces an out-of-bounds index into a `Vec` of length 1000, causing a Rust panic and crashing the node process.

---

### Finding Description

In `remove_unconfirmed_tx`:

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
    if tx_age >= self.block_unconfirmed_txs.len() {   // guards block_unconfirmed_txs only
        self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
    } else {
        let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
        self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
    }
    if count_failure {
        self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;  // ← NO BOUNDS CHECK
    }
}
``` [1](#0-0) 

The check at line 208 (`tx_age >= self.block_unconfirmed_txs.len()`) correctly handles the `block_unconfirmed_txs` ring-buffer access, but the `confirm_blocks_to_failed_txs` access at line 215 is completely unguarded. Both `block_unconfirmed_txs` and `confirm_blocks_to_failed_txs` are initialized with the same length `MAX_CONFIRM_BLOCKS = 1000`: [2](#0-1) 

So valid indices are `0..999`. When `tx_age >= 1001`, `tx_age - 1 >= 1000` is out of bounds, and Rust panics.

The `count_failure=true` path is triggered exclusively by `drop_tx` → `drop_tx_inner(tx_hash, true)`, which is called from `reject_tx`: [3](#0-2) [4](#0-3) 

Confirmed transactions go through `process_block_tx` → `drop_tx_inner(tx_hash, false)` (count_failure=false), so they never reach the vulnerable line. [5](#0-4) 

---

### Impact Explanation

When the panic fires inside the fee estimator (which runs in the node's main processing loop via `commit_block` / `reject_tx`), the CKB node process terminates. This is a **remote, unprivileged denial-of-service**: any peer or RPC caller who can submit a transaction to the tx pool can trigger it deterministically after 1001 blocks.

---

### Likelihood Explanation

CKB produces ~1 block per 8 seconds, so 1000 blocks ≈ 2.2 hours. The tx pool expiry is configured via `expiry_hours: u8` in `TxPoolConfig`: [6](#0-5) 

A `u8` allows up to 255 hours. Any default ≥ 3 hours (very likely) means a low-fee transaction submitted by an attacker will remain tracked in the fee estimator for 1000+ blocks before expiry. When the tx pool finally evicts it, `reject_tx` fires, `tx_age > 1000`, and the node panics. The attacker needs only to submit one valid (but low-fee) transaction and wait.

---

### Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` access:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard logic for `block_unconfirmed_txs` and silently discards failure samples for transactions older than `MAX_CONFIRM_BLOCKS`, which is consistent with the estimator's design intent (it only tracks statistics within the last 1000 blocks).

---

### Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let mut algo = Algorithm::new();
    algo.update_ibd_state(false); // mark ready

    // Simulate best_height = 0 via commit_block of an empty block at height 1
    // then track a tx at height 1
    // advance 1001 more blocks, then reject_tx → panic

    // Use TxConfirmStat directly for a minimal repro:
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993);

    // Track tx at entry_height=0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // tip_height = 1001 → tx_age = 1001 → tx_age - 1 = 1000 → OOB on Vec of len 1000
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true); // panics here
}
``` [7](#0-6)

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
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

**File:** util/app-config/src/configs/tx_pool.rs (L41-43)
```rust
    /// The expiration time for pool transactions in hours
    pub expiry_hours: u8,
}
```
