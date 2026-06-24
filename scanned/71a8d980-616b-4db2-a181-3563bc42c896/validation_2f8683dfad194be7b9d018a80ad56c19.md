Audit Report

## Title
Out-of-Bounds Index Panic in `TxConfirmStat::remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access with a `tx_age >= len()` check, but applies no equivalent guard before indexing `confirm_blocks_to_failed_txs[tx_age - 1]`. When a tracked transaction remains in the pool for more than `MAX_CONFIRM_BLOCKS` (1000) blocks and is then evicted with `count_failure = true`, `tx_age - 1 >= 1000` causes a Rust index-out-of-bounds panic, crashing the node process.

## Finding Description

`confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are both allocated with length `MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 protects only the `block_unconfirmed_txs` write: [2](#0-1) 

Execution then falls through unconditionally to:

```rust
self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
``` [3](#0-2) 

When `tx_age >= 1001`, `tx_age - 1 >= 1000` is out of bounds for the 1000-element `Vec`, causing a guaranteed panic.

The full call chain reaching this with `count_failure = true`:

- `reject_tx` calls `drop_tx`, which hardcodes `count_failure = true`: [4](#0-3) 
- `drop_tx` calls `drop_tx_inner` → `remove_unconfirmed_tx(..., true)`: [5](#0-4) 
- The reject callback (which calls `reject_tx`) is fired from both `remove_expired` and `limit_size`: [6](#0-5) [7](#0-6) 

## Impact Explanation

A Rust `Vec` index-out-of-bounds panics unconditionally in release builds. The tx-pool service runs as a Tokio task; an unhandled panic in that task terminates the process. The entire CKB node crashes. This matches the **High** impact: *Vulnerabilities which could easily crash a CKB node* (10001–15000 points).

## Likelihood Explanation

At ~10 s/block, 1001 blocks ≈ 2.78 hours — well within the default 12-hour expiry window. Any unprivileged peer can submit a single valid, minimum-fee-rate transaction via the standard P2P relay path. No hashpower, Sybil capability, or special privileges are required. The attacker simply submits one valid tx and waits; natural chain growth advances `best_height` past `entry_height + 1000`. Pool size eviction (`limit_size`) can accelerate the trigger to any time after 1001 blocks by flooding the pool with higher-fee-rate transactions.

## Recommendation

Add a bounds check before the `confirm_blocks_to_failed_txs` write, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This silently drops the failure sample for txs older than `MAX_CONFIRM_BLOCKS`, consistent with how `block_unconfirmed_txs` already handles the same case.

## Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_oob_panic() {
    let mut stat = TxConfirmStat::default(); // MAX_CONFIRM_BLOCKS = 1000

    // Simulate: tx tracked at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1000)).unwrap();

    // Advance the window 1001 times so best_height = 1001
    for h in 1..=1001u64 {
        stat.move_track_window(h);
    }

    // tx_age = 1001 - 0 = 1001; tx_age - 1 = 1000 → OOB on confirm_blocks_to_failed_txs (len=1000)
    // This panics:
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L427-430)
```rust
    /// tx removed from txpool
    fn drop_tx(&mut self, tx_hash: &Byte32) -> bool {
        self.drop_tx_inner(tx_hash, true).is_some()
    }
```

**File:** tx-pool/src/pool.rs (L281-287)
```rust
        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
```

**File:** tx-pool/src/pool.rs (L306-324)
```rust
            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
```
