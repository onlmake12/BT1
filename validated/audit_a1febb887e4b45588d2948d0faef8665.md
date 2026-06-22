### Title
Missing Bounds Check on `confirm_blocks_to_failed_txs` Array Index in `remove_unconfirmed_tx` Causes Node Panic — (`File: util/fee-estimator/src/estimator/confirmation_fraction.rs`)

---

### Summary

The `remove_unconfirmed_tx` function in the `ConfirmationFraction` fee estimator correctly guards the `block_unconfirmed_txs` array against out-of-bounds access when `tx_age >= MAX_CONFIRM_BLOCKS`, but then unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` without the same guard. When a tracked transaction is rejected from the tx-pool after more than `MAX_CONFIRM_BLOCKS` (1000) blocks, `tx_age - 1 >= 1000` exceeds the array length, triggering a Rust index-out-of-bounds panic that crashes the node process.

---

### Finding Description

`TxConfirmStat` allocates both `block_unconfirmed_txs` and `confirm_blocks_to_failed_txs` with length `MAX_CONFIRM_BLOCKS = 1000`: [1](#0-0) [2](#0-1) 

In `remove_unconfirmed_tx`, the code correctly detects when `tx_age >= self.block_unconfirmed_txs.len()` (i.e., `tx_age >= 1000`) and routes to `old_unconfirmed_txs` instead of indexing `block_unconfirmed_txs`. However, immediately after, it unconditionally indexes `confirm_blocks_to_failed_txs[tx_age - 1]` with no equivalent guard: [3](#0-2) 

When `tx_age = 1001` (or higher), `tx_age - 1 = 1000` which is `>= confirm_blocks_to_failed_txs.len()` (1000), causing a Rust index-out-of-bounds **panic** and crashing the node process.

The `count_failure = true` path is reached via `drop_tx` → `drop_tx_inner(tx_hash, true)`, which is called by `reject_tx`: [4](#0-3) 

The reject callback in `shared_builder.rs` wires every tx-pool rejection to `fee_estimator.reject_tx`: [5](#0-4) 

---

### Impact Explanation

When the `ConfirmationFraction` fee estimator is enabled and a tracked transaction is rejected from the tx-pool after more than 1000 blocks, the node panics and terminates. This is a **remote denial-of-service**: any unprivileged tx-pool submitter can crash a node that has this estimator configured, with no recovery until the operator restarts the process.

---

### Likelihood Explanation

The `ConfirmationFraction` algorithm is a documented, supported configuration option: [6](#0-5) 

An attacker needs only to:
1. Submit a low-fee transaction (accepted into the pool and tracked at height H).
2. Wait 1001+ blocks (~2.8 hours at 10 s/block) — or accelerate eviction by flooding the pool with higher-fee transactions to trigger `Reject::Full`.
3. Submit a conflicting (double-spend) transaction with a higher fee, causing the original tracked tx to be rejected via `Reject::RBFRejected` or `Reject::Invalidated`.

Step 3 is fully within the attacker's control since they own the private key for the UTXO they spent in step 1. The `limit_size` eviction path also calls `callbacks.call_reject` unconditionally: [7](#0-6) 

---

### Recommendation

Add a bounds check before indexing `confirm_blocks_to_failed_txs`, mirroring the existing guard for `block_unconfirmed_txs`:

```rust
if count_failure {
    if let Some(slot) = self.confirm_blocks_to_failed_txs.get_mut(tx_age - 1) {
        slot[bucket_index] += 1f64;
    }
}
```

This is directly analogous to the reported oracle bug: the code correctly uses a length-based guard for one array but omits it for the parallel array indexed with the same value. [8](#0-7) 

---

### Proof of Concept

1. Configure the node with `fee_estimator.algorithm = "ConfirmationFraction"`.
2. Submit a transaction `tx1` spending a UTXO you control. The estimator tracks it at `entry_height = H`.
3. Mine or wait for 1001 blocks so `best_height = H + 1001`.
4. Submit `tx2` double-spending the same UTXO with a higher fee rate. The tx-pool accepts `tx2` and rejects `tx1` via `Reject::RBFRejected` (or flood the pool to trigger `Reject::Full`).
5. The reject callback fires `fee_estimator.reject_tx(&tx1_hash)` → `drop_tx(tx1_hash)` → `drop_tx_inner(tx1_hash, true)` → `remove_unconfirmed_tx(H, H+1001, bucket_index, true)`.
6. `tx_age = 1001 >= 1000` → enters the `old_unconfirmed_txs` branch correctly, then hits `self.confirm_blocks_to_failed_txs[1000][bucket_index]` — **index out of bounds, node panics**. [9](#0-8) [4](#0-3) [10](#0-9)

### Citations

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L21-22)
```rust
/// The number of blocks that the estimator will trace the statistics.
const MAX_CONFIRM_BLOCKS: usize = 1000;
```

**File:** util/fee-estimator/src/estimator/confirmation_fraction.rs (L139-141)
```rust
        let confirm_blocks_to_confirmed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let confirm_blocks_to_failed_txs = vec![vec![0f64; buckets.len()]; max_confirm_blocks];
        let block_unconfirmed_txs = vec![vec![0; buckets.len()]; max_confirm_blocks];
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

**File:** util/app-config/src/configs/fee_estimator.rs (L13-16)
```rust
pub enum Algorithm {
    /// Confirmation Fraction Fee Estimator
    ConfirmationFraction,
    /// Weight-Units Flow Fee Estimator
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
