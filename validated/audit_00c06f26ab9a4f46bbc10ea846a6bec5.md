Audit Report

## Title
Unguarded out-of-bounds index in `TxConfirmStat::remove_unconfirmed_tx` causes node panic when `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`remove_unconfirmed_tx` correctly bounds-checks the `block_unconfirmed_txs` access when `tx_age >= 1000`, but leaves the immediately following `confirm_blocks_to_failed_txs[tx_age - 1]` write completely unguarded. When a tracked transaction ages past 1000 blocks and is then evicted from the tx-pool via `reject_tx`, Rust's `Vec` indexing panics unconditionally, terminating the node process.

## Finding Description
`confirm_blocks_to_failed_txs` is initialized with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices `0..=999`) at line 140. [1](#0-0) 

Inside `remove_unconfirmed_tx` (lines 208–213), the code detects `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `>= 1000`) and redirects the unconfirmed-count decrement to `old_unconfirmed_txs`. However, at line 215, the write `self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64` executes unconditionally whenever `count_failure` is true, with no check that `tx_age - 1 < 1000`. [2](#0-1) 

For `tx_age = 1001`, the index `1000` is out of bounds, causing a Rust panic. The full call chain is: `reject_tx` (line 475) → `drop_tx` (line 428, `count_failure = true`) → `drop_tx_inner` (line 416) → `remove_unconfirmed_tx(..., count_failure = true)` (line 418). [3](#0-2) 

`tx_age` is computed as `tip_height.saturating_sub(entry_height)`, where `entry_height` is set to `current_tip` at the time `accept_tx` is called, and `tip_height` is `best_height` updated by each subsequent `commit_block`. [4](#0-3)  After 1001 blocks pass without the tx being confirmed, `tx_age` reaches 1001 and the next `reject_tx` call panics. [5](#0-4) 

## Impact Explanation
A Rust index-out-of-bounds panic in release builds terminates the node process entirely. This matches the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
Any unprivileged user can submit a single valid but low-fee transaction. If the tx-pool's eviction policy allows the transaction to remain unconfirmed for more than 1000 blocks (plausible for sufficiently low fee rates), the next call to `reject_tx` for that transaction deterministically panics the node. No special privilege, key, or hashpower is required. The attack is repeatable.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure {
    if let Some(row) = self.confirm_blocks_to_failed_txs.get_mut(tx_age - 1) {
        row[bucket_index] += 1f64;
    }
    // silently discard the sample when tx_age > MAX_CONFIRM_BLOCKS
}
```

This is consistent with the existing design intent: transactions older than `MAX_CONFIRM_BLOCKS` are already treated as "old" for the unconfirmed-count tracking; failure samples beyond the tracking window should likewise be discarded. [2](#0-1) 

## Proof of Concept
1. Instantiate `Algorithm::new()`, call `update_ibd_state(false)` to mark it ready.
2. Commit a block at height `H` to set `current_tip = H` and `best_height = H`.
3. Call `accept_tx(tx_hash, info)` — the tx is tracked at height `H`.
4. Commit 1001 more blocks (heights `H+1` through `H+1001`) without including `tx_hash` — `best_height` advances to `H+1001`, so `tx_age = 1001`.
5. Call `reject_tx(&tx_hash)`.
6. **Expected (before fix):** `panic: index out of bounds: the len is 1000 but the index is 1000`.
7. **Expected (after fix):** returns normally.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-140)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L204-204)
```rust
        let tx_age = tip_height.saturating_sub(entry_height) as usize;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L208-216)
```rust
        if tx_age >= self.block_unconfirmed_txs.len() {
            self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
        } else {
            let block_index = (entry_height % self.block_unconfirmed_txs.len() as u64) as usize;
            self.block_unconfirmed_txs[block_index][bucket_index] -= 1;
        }
        if count_failure {
            self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
        }
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L377-382)
```rust
    fn process_block(&mut self, height: u64, txs: impl Iterator<Item = Byte32>) {
        // For simpfy, we assume chain reorg will not effect tx fee.
        if height <= self.best_height {
            return;
        }
        self.best_height = height;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L416-430)
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

    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```
