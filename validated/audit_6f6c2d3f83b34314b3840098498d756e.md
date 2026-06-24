Audit Report

## Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` array access at line 208 but applies no equivalent guard before indexing `confirm_blocks_to_failed_txs[tx_age - 1]` at line 215. When a tracked transaction is rejected after more than 1000 blocks in the pool, `tx_age - 1 >= 1000` exceeds the array's valid range (0–999), causing an index-out-of-bounds **panic** that crashes the node process. This is remotely triggerable by any unprivileged user.

## Finding Description

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS = 1000` rows (valid indices 0–999): [1](#0-0) [2](#0-1) 

In `remove_unconfirmed_tx`, the guard at line 208 only protects the `block_unconfirmed_txs` write. After that branch, the `count_failure` block at lines 214–215 uses `tx_age - 1` as an unchecked index into `confirm_blocks_to_failed_txs` with no bounds guard: [3](#0-2) 

When `tx_age >= 1001`, execution takes the `old_unconfirmed_txs` branch (line 209) and then falls through to line 214. With `tx_age = 1001`, `tx_age - 1 = 1000` is out of bounds for a 1000-element array → **panic**.

The full call chain is confirmed in production code:

- Any tx rejection fires the registered reject callback in `shared_builder.rs`, which unconditionally calls `fee_estimator.reject_tx(&tx_hash)`: [4](#0-3) 

- `reject_tx` calls `drop_tx`, which calls `drop_tx_inner(count_failure=true)`: [5](#0-4) 

- `drop_tx_inner` calls `remove_unconfirmed_tx` with `count_failure=true`: [6](#0-5) 

## Impact Explanation

A Rust index-out-of-bounds panic aborts the process. Any CKB node configured with the `ConfirmationFraction` fee estimator will crash when any tracked transaction older than 1000 blocks is evicted from the pool. This is a remotely triggerable, unprivileged **node crash (DoS)**, matching the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

At ~8 s/block, 1001 blocks ≈ 2.2 hours — well within the default 72-hour tx expiry window. A low-fee transaction submitted by any user naturally ages past 1000 blocks on a congested network. Eviction triggers include pool-full eviction (`limit_size`), expiry, RBF conflict, or chain reorg invalidation — all of which call the reject callback unconditionally. No special privilege, key, or hashpower is required. A single valid low-fee transaction submission is sufficient to trigger the crash.

## Recommendation

Add a bounds check before the `count_failure` write, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure {
    if let Some(row) = self.confirm_blocks_to_failed_txs.get_mut(tx_age - 1) {
        row[bucket_index] += 1f64;
    }
    // silently drop samples for txs older than MAX_CONFIRM_BLOCKS
}
```

## Proof of Concept

```rust
#[test]
fn test_remove_unconfirmed_tx_no_panic_for_old_txs() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let max_confirm_blocks = 1000;
    let decay = 0.993f64;
    let mut stat = TxConfirmStat::new(buckets, max_confirm_blocks, decay);

    stat.add_unconfirmed_tx(0, FeeRate::from_u64(1500));

    for h in 1..=1000u64 {
        stat.move_track_window(h);
    }
    stat.bucket_stats[0].old_unconfirmed_txs += 1;

    // tx_age = 1001: index 1000 — OUT OF BOUNDS → panic on unpatched code
    stat.remove_unconfirmed_tx(0, 1001, 0, true);
}
```

Running this test against the unpatched code panics at `tx_age = 1001` with `index out of bounds: the len is 1000 but the index is 1000`. [7](#0-6)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L22-22)
```rust
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L140-140)
```rust
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
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

**File:** shared/src/shared_builder.rs (L576-601)
```rust
    tx_pool_builder.register_reject(Box::new(
        move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
            let tx_hash = entry.transaction().hash();
            // record recent reject
            if reject.should_recorded()
                && let Some(ref mut recent_reject) = tx_pool.recent_reject
                && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
            {
                error!("record recent_reject failed {} {} {}", tx_hash, reject, e);
            }

            if reject.is_allowed_relay()
                && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject {
                    tx_hash: tx_hash.clone(),
                })
            {
                error!("tx-pool tx_relay_sender internal error {}", e);
            }

            // notify
            let notify_tx_entry = create_notify_entry(entry);
            notify_reject.notify_reject_transaction(notify_tx_entry, reject);

            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
        },
```
