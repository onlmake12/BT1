Audit Report

## Title
Out-of-bounds index panic in `TxConfirmStat::remove_unconfirmed_tx` when `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices `0..=999`). When a tracked transaction ages more than 1000 blocks and is then evicted from the tx-pool, `remove_unconfirmed_tx` computes `tx_age - 1 >= 1000` and indexes past the end of the Vec, causing an unconditional Rust index-out-of-bounds panic that terminates the node process.

## Finding Description

`confirm_blocks_to_failed_txs` is initialized with exactly `max_confirm_blocks` (= 1000) rows: [1](#0-0) 

Inside `remove_unconfirmed_tx`, the code correctly guards the `block_unconfirmed_txs` access by redirecting to `old_unconfirmed_txs` when `tx_age >= 1000`: [2](#0-1) 

However, the immediately following `confirm_blocks_to_failed_txs` write carries no corresponding guard: [3](#0-2) 

When `tx_age = 1001`, `tx_age - 1 = 1000` exceeds the last valid index (999). Rust's `Vec` indexing panics unconditionally on out-of-bounds access, even in release builds.

The full call chain that sets `count_failure = true`:

- `reject_tx` (line 475) → `drop_tx` (line 428) → `drop_tx_inner(tx_hash, true)` (line 429) → `remove_unconfirmed_tx(..., count_failure = true)` (line 418) [4](#0-3) 

## Impact Explanation

A node process panic terminates the CKB node entirely. This matches the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

An attacker submits one valid but low-fee transaction to the target node's tx-pool. If the transaction is not confirmed and not evicted within 1000 blocks (~2–3 hours at ~8–10 s/block), the fee estimator continues tracking it. When the tx-pool eventually evicts it (calling `reject_tx`), the panic is triggered deterministically. No special privilege, key, or hashpower is required — only a valid transaction and patience. The attack is repeatable.

## Recommendation

Mirror the existing `block_unconfirmed_txs` guard before the `confirm_blocks_to_failed_txs` write:

```rust
if count_failure {
    if let Some(row) = self.confirm_blocks_to_failed_txs.get_mut(tx_age - 1) {
        row[bucket_index] += 1f64;
    }
    // silently discard failure samples beyond the tracking window
}
```

This is consistent with the existing design: transactions older than `MAX_CONFIRM_BLOCKS` are already treated as "old" for the unconfirmed-count path; failure samples beyond the window should likewise be discarded.

## Proof of Concept

```rust
#[test]
fn test_no_panic_on_old_failed_tx() {
    let mut algo = Algorithm::new();
    algo.update_ibd_state(false);

    // Accept a tx at current_tip = 0 (requires advancing best_height first)
    // Advance best_height to 1 by committing a block, then accept at tip=1
    algo.commit_block(&make_block(1));
    let tx_hash = ckb_types::packed::Byte32::default();
    let info = TxEntryInfo { fee: 1000.into(), size: 100, cycles: 1000 };
    algo.accept_tx(tx_hash.clone(), info); // tracked at height=1

    // Advance 1001 more blocks without confirming the tx → tx_age = 1001
    for h in 2u64..=1002 {
        algo.commit_block(&make_block(h));
    }

    // Before the fix: panics with index out of bounds (index 1000, len 1000)
    algo.reject_tx(&tx_hash);
}
```

`tx_age - 1 = 1000` is not `< confirm_blocks_to_failed_txs.len()` (= 1000), proving the out-of-bounds access. [5](#0-4)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-140)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L204-216)
```rust
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
