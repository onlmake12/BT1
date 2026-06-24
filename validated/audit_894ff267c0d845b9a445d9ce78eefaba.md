The code is confirmed. Let me verify the exact lines:

- [1](#0-0)  allocates `confirm_blocks_to_failed_txs` with exactly `max_confirm_blocks` (= 1000) rows, valid indices `0..=999`.
- [2](#0-1)  guards `block_unconfirmed_txs` when `tx_age >= len`, but the immediately following write at [3](#0-2)  has no such guard.
- The call chain via `reject_tx` → `drop_tx` → `drop_tx_inner(..., true)` is confirmed at [4](#0-3) .

---

Audit Report

## Title
Out-of-bounds index panic in `TxConfirmStat::remove_unconfirmed_tx` when `tx_age > MAX_CONFIRM_BLOCKS` — (File: `util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary
`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS` (1000) rows, giving valid indices `0..=999`. When a tracked transaction ages beyond 1000 blocks and is then evicted from the tx-pool via `reject_tx`, `remove_unconfirmed_tx` computes `tx_age - 1 >= 1000` and indexes past the end of the `Vec`, causing an unconditional Rust index-out-of-bounds panic that terminates the node process.

## Finding Description
`confirm_blocks_to_failed_txs` is initialized with `max_confirm_blocks` rows:

```rust
let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
```

In `remove_unconfirmed_tx`, the `block_unconfirmed_txs` access is correctly guarded:

```rust
if tx_age >= self.block_unconfirmed_txs.len() {
    self.bucket_stats[bucket_index].old_unconfirmed_txs -= 1;
} else {
    ...
}
```

But the immediately following write carries no corresponding guard:

```rust
if count_failure {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

When `tx_age = 1001`, `tx_age - 1 = 1000` exceeds the last valid index (999). Rust's `Vec` indexing panics unconditionally on out-of-bounds access in both debug and release builds.

The full reachable call chain with `count_failure = true`:
- `reject_tx` (L475) → `drop_tx` (L428) → `drop_tx_inner(tx_hash, true)` (L429) → `remove_unconfirmed_tx(..., count_failure = true)` (L418)

No guard exists anywhere in this chain to prevent the out-of-bounds access.

## Impact Explanation
A Rust index-out-of-bounds panic terminates the entire CKB node process. This matches the allowed bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation
An unprivileged attacker submits one valid but low-fee transaction to the target node's tx-pool. No confirmation is needed — the attacker only needs the transaction to remain unconfirmed and untracked for more than 1000 blocks (~2–3 hours at ~8–10 s/block). When the tx-pool eventually evicts it (calling `reject_tx`), the panic is triggered deterministically. No special privilege, key, or hashpower is required. The attack is repeatable and requires only a valid transaction and patience.

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

`tx_age - 1 = 1000` is not `< confirm_blocks_to_failed_txs.len()` (= 1000), proving the out-of-bounds access deterministically.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-140)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
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
