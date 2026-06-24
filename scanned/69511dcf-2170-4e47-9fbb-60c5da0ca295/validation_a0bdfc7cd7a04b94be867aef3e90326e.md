Audit Report

## Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

## Summary

`TxConfirmStat::remove_unconfirmed_tx` guards the `block_unconfirmed_txs` access with a bounds check (`tx_age >= self.block_unconfirmed_txs.len()`), but then unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without any corresponding guard. Both vectors have length `MAX_CONFIRM_BLOCKS = 1000`. When a tracked transaction is evicted after more than 1000 blocks, `tx_age - 1 >= 1000` exceeds the last valid index (999), causing a Rust index-out-of-bounds panic that crashes the node process.

## Finding Description

Both `confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are allocated with `max_confirm_blocks = 1000` entries: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 routes to `old_unconfirmed_txs` when `tx_age >= 1000`, correctly protecting the `block_unconfirmed_txs` access. However, the `count_failure` block at lines 214–216 is **outside** that conditional and has no bounds check: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is a valid Rust index expression but exceeds the vector's last valid index (999), causing an immediate `index out of bounds` panic.

`drop_tx` always passes `count_failure = true`: [3](#0-2) 

`reject_tx` calls `drop_tx`: [4](#0-3) 

The reject callback registered in production wires every tx-pool rejection directly to `fee_estimator.reject_tx`: [5](#0-4) 

`remove_expired` calls `callbacks.call_reject` for every expired entry: [6](#0-5) 

`track_tx` only records a tx when `height == self.best_height`, so `tx_age` grows monotonically as the chain advances: [7](#0-6) 

## Impact Explanation

A Rust out-of-bounds vector index panics the process unconditionally. The fee estimator runs inside the tx-pool service thread. A panic there crashes the CKB node process entirely. This constitutes **remote Denial of Service against a single CKB node**, matching the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation

At ~28 seconds per block, 1001 blocks ≈ 7.8 hours. The default tx-pool expiry is 12 hours (time-based, not block-based). A low-fee-rate transaction that is valid but never proposed will survive in the pool past the 1000-block threshold and be evicted by `remove_expired`, triggering the panic. No special privileges are required — any peer can submit a valid transaction with a fee rate above the minimum but below any miner's threshold. The attacker does not need to control hashpower or any node configuration. The condition is reliably reproducible on any node running the `ConfirmationFraction` fee estimator with a live chain.

## Recommendation

Add a bounds check before indexing `confirm_blocks_to_failed_txs`, mirroring the existing guard on `block_unconfirmed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This silently drops the failure sample for transactions older than the tracking window, which is the correct semantic — they are already accounted for via `old_unconfirmed_txs`.

## Proof of Concept

1. Start a CKB node with the `ConfirmationFraction` fee estimator enabled.
2. Submit a valid transaction at tip height `H` with a fee rate just above the minimum but below any miner's threshold (so it is never proposed).
3. Advance the chain by 1001 blocks without including the transaction (e.g., on a private testnet or by waiting on mainnet).
4. Wait for `remove_expired` to fire (default 12-hour expiry), which calls `callbacks.call_reject` → `fee_estimator.reject_tx` → `drop_tx(count_failure=true)` → `remove_unconfirmed_tx` with `tx_age = 1001`.
5. Observe: `thread 'tokio-runtime-worker' panicked at 'index out of bounds: the len is 1000 but the index is 1000'` at `confirmation_fraction.rs:215`, crashing the node.

Alternatively, a unit test can reproduce this directly:
```rust
let mut stat = TxConfirmStat::new(buckets, 1000, decay);
stat.add_unconfirmed_tx(0, fee_rate); // entry_height = 0
// simulate tip_height = 1001, tx_age = 1001 > 1000
stat.remove_unconfirmed_tx(0, 1001, bucket_index, true); // panics
```

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

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L400-403)
```rust
        if height != self.best_height {
            // ignore wrong height txs
            return;
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

**File:** shared/src/shared_builder.rs (L599-600)
```rust
            // fee estimator
            fee_estimator.reject_tx(&tx_hash);
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
