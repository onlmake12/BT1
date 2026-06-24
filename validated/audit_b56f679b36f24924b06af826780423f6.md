Audit Report

## Title
Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` via Long-Lived Unconfirmed Transaction Eviction — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` entries. In `remove_unconfirmed_tx`, the existing bounds guard only protects the `block_unconfirmed_txs` decrement; the subsequent `confirm_blocks_to_failed_txs[tx_age - 1]` access is completely unguarded. When a tracked transaction is evicted after more than 1000 blocks, `tx_age - 1 >= 1000` produces an out-of-bounds index on a 1000-element `Vec`, causing a Rust panic that crashes the node process.

## Finding Description

`confirm_blocks_to_failed_txs` is initialized with exactly `max_confirm_blocks` (1000) rows: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 checks `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `>= 1000`) and routes the unconfirmed-tracking decrement to `old_unconfirmed_txs`. However, execution continues unconditionally to the `count_failure` branch: [2](#0-1) 

When `tx_age = 1001`, the guard at line 208 is satisfied (correctly handling `block_unconfirmed_txs`), but line 215 then accesses `confirm_blocks_to_failed_txs[1000]` on a Vec of length 1000 — index 1000 is out of bounds. Rust's `Vec` indexing panics unconditionally on OOB access in both debug and release builds, aborting the process.

The eviction path that triggers this with `count_failure = true` is: [3](#0-2) 

`drop_tx` always passes `count_failure = true`: [4](#0-3) 

And `reject_tx` (the public API called by the tx pool on eviction) calls `drop_tx`: [5](#0-4) 

## Impact Explanation

A Rust index-out-of-bounds panic aborts the node process. This is a **remote, unprivileged node crash**, matching the allowed CKB bounty impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The `ConfirmationFraction` estimator is a real production variant: [6](#0-5) 

An attacker submits a valid transaction with a fee rate low enough to avoid being mined but high enough to be accepted into the pool. The default tx pool expiry is 12 hours (~4320 blocks at 10 s/block), which far exceeds the 1000-block threshold. No special privileges are required — only a valid RPC transaction submission. The crash is deterministic and repeatable.

## Recommendation

Add a bounds guard before the `confirm_blocks_to_failed_txs` index access in `remove_unconfirmed_tx`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard already applied to `block_unconfirmed_txs` at line 208, and is consistent with the semantics that transactions older than `MAX_CONFIRM_BLOCKS` are already classified as `old_unconfirmed_txs` and should not be indexed into the fixed-size failure table.

## Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    // Track a tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // tx_age = 1001: confirm_blocks_to_failed_txs[1000] on a len-1000 Vec → panic
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

`tx_age = 1001`, `tx_age - 1 = 1000`, `confirm_blocks_to_failed_txs.len() = 1000` → **index out of bounds: the len is 1000 but the index is 1000** → node crash.

### Citations

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

**File:** util/fee-estimator/src/estimator/mod.rs (L35-38)
```rust
    pub fn new_confirmation_fraction() -> Self {
        let algo = confirmation_fraction::Algorithm::new();
        FeeEstimator::ConfirmationFraction(Arc::new(RwLock::new(algo)))
    }
```
