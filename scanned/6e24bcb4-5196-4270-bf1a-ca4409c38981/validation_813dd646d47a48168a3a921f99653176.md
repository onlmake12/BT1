The code is fully readable. Let me trace the exact logic in `remove_unconfirmed_tx`.

The vulnerability is real and concrete. Here is the full analysis:

---

### Title
Out-of-Bounds Panic in `remove_unconfirmed_tx` When `tx_age > MAX_CONFIRM_BLOCKS` — (`util/fee-estimator/src/estimator/confirmation_fraction.rs`)

### Summary

`TxConfirmStat::remove_unconfirmed_tx` correctly branches on `tx_age >= block_unconfirmed_txs.len()` to handle the `old_unconfirmed_txs` counter, but then unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without a bounds check. Both vectors have length `MAX_CONFIRM_BLOCKS = 1000`. When `tx_age > 1000`, the index `tx_age - 1 >= 1000` is out of bounds, causing a Rust panic and crashing the node.

### Finding Description

`confirm_blocks_to_failed_txs` and `block_unconfirmed_txs` are both allocated with length `MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) 

In `remove_unconfirmed_tx`, the guard at line 208 correctly routes to `old_unconfirmed_txs` when `tx_age >= 1000`, but the `count_failure` block at line 214–216 is **outside** that conditional and has no bounds check: [2](#0-1) 

When `tx_age = 1001`, `tx_age - 1 = 1000` is a valid Rust index expression but exceeds the vector's last valid index (999), causing an immediate `index out of bounds` panic.

`drop_tx` always passes `count_failure = true`: [3](#0-2) 

`reject_tx` calls `drop_tx`: [4](#0-3) 

The reject callback registered in production wires every tx-pool rejection directly to `fee_estimator.reject_tx`: [5](#0-4) 

All tx-pool eviction paths (`remove_expired`, `limit_size`, RBF replacement, conflict resolution) call `callbacks.call_reject`, which triggers this path: [6](#0-5) 

### Impact Explanation

A Rust out-of-bounds vector index panics the process unconditionally. The fee estimator runs inside the tx-pool service thread. A panic there crashes the CKB node process entirely, constituting a **remote Denial of Service**.

### Likelihood Explanation

At ~28 seconds per block, 1001 blocks ≈ 7.8 hours. The default tx-pool expiry is 12 hours (time-based, not block-based). A low-fee-rate transaction that is never proposed will survive in the pool past the 1000-block threshold and be evicted by `remove_expired`, triggering the panic. No special privileges are required — any peer can submit a valid transaction with a fee rate above the minimum but low enough to never be proposed. The attacker does not need to control hashpower or any node configuration.

### Recommendation

Add a bounds check before indexing `confirm_blocks_to_failed_txs`:

```rust
if count_failure && tx_age <= self.confirm_blocks_to_failed_txs.len() {
    self.confirm_blocks_to_failed_txs[tx_age - 1][bucket_index] += 1f64;
}
```

This mirrors the existing guard on `block_unconfirmed_txs` and silently drops the failure sample for transactions older than the tracking window, which is the correct semantic (they are already counted as `old_unconfirmed_txs`).

### Proof of Concept

1. Start a CKB node with `ConfirmationFraction` fee estimator enabled.
2. Submit a valid transaction at tip height `H` with a fee rate just above the minimum but below any miner's threshold (so it is never proposed).
3. Advance the chain by 1001 blocks without including the transaction.
4. Wait for `remove_expired` to fire (or trigger `limit_size` by flooding the pool), which calls `callbacks.call_reject` → `fee_estimator.reject_tx`.
5. Observe: `thread 'tokio-runtime-worker' panicked at 'index out of bounds: the len is 1000 but the index is 1000'` in `confirmation_fraction.rs:215`, crashing the node.

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

**File:** tx-pool/src/pool.rs (L271-287)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
```
