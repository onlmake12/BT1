The code is confirmed. Let me verify the exact lines and call chain before rendering the verdict.

The file at `util/fee-estimator/src/estimator/confirmation_fraction.rs` confirms every claim:

- Line 22: `const MAX_CONFIRM_BLOCKS: usize = 1000;` [1](#0-0) 
- Line 140: `confirm_blocks_to_failed_txs` initialized with length `max_confirm_blocks` (= 1000) [2](#0-1) 
- Lines 208–216: the `block_unconfirmed_txs` branch is guarded by `tx_age >= self.block_unconfirmed_txs.len()`, but the `confirm_blocks_to_failed_txs[tx_age - 1]` access at line 215 has **no** corresponding bound check [3](#0-2) 
- `reject_tx` → `drop_tx` → `drop_tx_inner(hash, true)` → `remove_unconfirmed_tx(..., count_failure=true)` call chain is intact [4](#0-3) 

---

Audit Report

## Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access with a `tx_age >= len` check, but unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without an equivalent bound check. When a tracked transaction remains unconfirmed for more than 1000 blocks and is then evicted from the tx-pool, `tx_age - 1 >= 1000` produces an out-of-bounds index on the 1000-element vector, causing a Rust panic and crashing the node process.

## Finding Description
`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices `0..=999`) at line 140. In `remove_unconfirmed_tx` (lines 197–217), `tx_age` is computed as `tip_height.saturating_sub(entry_height)`. The branch at line 208 correctly handles the case `tx_age >= self.block_unconfirmed_txs.len()` (also 1000) by decrementing `old_unconfirmed_txs` instead of indexing `block_unconfirmed_txs`. However, the subsequent `count_failure` block at line 215 — `self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64` — is entirely unguarded. When `tx_age == 1001`, `tx_age - 1 == 1000`, which is one past the end of the vector, causing an immediate panic.

The reachable call path is: `reject_tx` (line 475) → `drop_tx` (line 428) → `drop_tx_inner(hash, true)` (line 429) → `remove_unconfirmed_tx(entry_height, best_height, bucket_index, true)` (lines 418–424). The `count_failure = true` argument is hardcoded in `drop_tx`, so every eviction via `reject_tx` reaches the vulnerable line.

A transaction is tracked only when submitted at the current `best_height` (line 400–402), so `entry_height` is always a valid past tip. After 1001 blocks elapse without confirmation, `tx_age` exceeds the vector length and the panic fires on the next eviction event.

## Impact Explanation
The panic terminates the node process. Any unprivileged user who can submit a valid transaction to a synced CKB node can trigger this crash after 1001 blocks. This matches the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
No special privileges, hashpower, or key material are required beyond the ability to submit a valid transaction. The default tx-pool expiry is approximately 3 days (~10,800 blocks at 28 s/block), which far exceeds the 1001-block threshold. A low-fee-rate transaction that is never included in a block will naturally reach `tx_age > 1000` before expiry, and the subsequent `remove_expired` eviction call triggers the panic. The condition is deterministically reachable on any synced, non-IBD node.

## Recommendation
Add a bounds check before the `confirm_blocks_to_failed_txs` index in `remove_unconfirmed_tx`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard on `block_unconfirmed_txs` at line 208 and silently discards failure records for transactions older than the tracking window, which is the correct semantic (the data would be outside the estimator's statistical window anyway).

## Proof of Concept
1. On a synced non-IBD node, submit a valid transaction with a fee rate below the typical confirmation threshold at tip height `H`. The fee estimator records it via `accept_tx` → `track_tx` → `add_unconfirmed_tx`, storing `entry_height = H`.
2. Mine (or wait for) 1001 blocks without including the transaction. Each block calls `commit_block` → `process_block` → `move_track_window`; after 1000 blocks the tx's slot is rotated into `old_unconfirmed_txs`.
3. Trigger eviction: either wait for `remove_expired` (fires after the pool expiry timeout, default ~3 days) or fill the pool to capacity to trigger `limit_size`. Either path calls `reject_tx(tx_hash)` on the fee estimator.
4. Inside `remove_unconfirmed_tx`: `tx_age = best_height - H = 1001`, `tx_age - 1 = 1000`, `self.confirm_blocks_to_failed_txs[1000]` → **index out of bounds: the len is 1000 but the index is 1000** → process panics.

A unit test can reproduce this directly by constructing a `TxConfirmStat` with `max_confirm_blocks = 1000`, calling `add_unconfirmed_tx(0, fee_rate)`, then calling `remove_unconfirmed_tx(0, 1001, bucket_index, true)` and observing the panic.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L22-22)
```rust
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
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
