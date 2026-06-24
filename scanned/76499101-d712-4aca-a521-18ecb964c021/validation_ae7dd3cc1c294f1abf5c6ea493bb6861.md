Audit Report

## Title
Index-Out-of-Bounds Panic in `TxConfirmStat::remove_unconfirmed_tx` via Missing Bounds Check on `confirm_blocks_to_failed_txs` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`confirm_blocks_to_failed_txs` is allocated with exactly `MAX_CONFIRM_BLOCKS` (1000) entries. In `remove_unconfirmed_tx`, the guard protecting `block_unconfirmed_txs` from out-of-bounds access is not applied to the subsequent `count_failure` branch, which indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without any length check. When a tracked transaction is evicted after 1001+ blocks, Rust panics unconditionally, crashing the node process.

## Finding Description

`confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are both allocated with `max_confirm_blocks = 1000` entries: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 checks `tx_age >= self.block_unconfirmed_txs.len()` and routes to `old_unconfirmed_txs` — but this guard only covers the `block_unconfirmed_txs` decrement. The `count_failure` branch immediately after has no corresponding bounds check: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000`, which is out of bounds for a vector of length 1000. Rust panics with `index out of bounds: the len is 1000 but the index is 1000`, terminating the process.

The call chain is: `reject_tx` → `drop_tx` → `drop_tx_inner(hash, true)` → `remove_unconfirmed_tx(..., count_failure=true)`: [3](#0-2) 

`reject_tx` is invoked from the `call_reject` callback registered in `shared_builder.rs` for every rejected/evicted transaction. Both `remove_expired` and `limit_size` in `tx-pool/src/pool.rs` call `callbacks.call_reject`: [4](#0-3) [5](#0-4) 

## Impact Explanation

A Rust index-out-of-bounds panic is unrecoverable and aborts the process. Any CKB node running the `ConfirmationFraction` fee estimator that evicts a transaction aged ≥ 1001 blocks will crash. This matches the **High** impact class: "Vulnerabilities which could easily crash a CKB node."

## Likelihood Explanation

The tx-pool default expiry is 12 hours (`DEFAULT_EXPIRY_HOURS = 12`). At CKB's ~10-second block time, 12 hours ≈ 4320 blocks, far exceeding the 1001-block threshold. Every node that runs long enough to expire a tracked transaction will trigger this panic automatically — no attacker required for the natural path. An adversarial path is also available: submit a valid low-fee transaction, wait 1001+ blocks (~2.8 hours), then flood the pool with higher-fee transactions to trigger `limit_size` eviction. The panic fires deterministically on any node with the fee estimator enabled.

## Recommendation

Add a bounds check before the `count_failure` branch in `remove_unconfirmed_tx`, consistent with how `add_confirmed_tx` caps at `max_confirms()`:

```rust
if count_failure && tx_age - 1 < self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

Transactions aged beyond `MAX_CONFIRM_BLOCKS` should be silently dropped from failure accounting, as they are already handled by the `old_unconfirmed_txs` counter.

## Proof of Concept

```rust
#[test]
fn test_oob_panic() {
    let buckets = vec![FeeRate::from_u64(1000), FeeRate::from_u64(2000)];
    let mut stat = TxConfirmStat::new(buckets, 1000, 0.993);
    // Track tx at height 0
    let bucket_index = stat.add_unconfirmed_tx(0, FeeRate::from_u64(1500)).unwrap();
    // tx_age = 1001 - 0 = 1001; confirm_blocks_to_failed_txs[1000] → PANIC
    stat.remove_unconfirmed_tx(0, 1001, bucket_index, true);
}
```

Running this test panics: `index out of bounds: the len is 1000 but the index is 1000`. The natural trigger (no attacker needed) is any node that runs for 12+ hours with a tracked low-fee transaction that expires via `remove_expired`.

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
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
