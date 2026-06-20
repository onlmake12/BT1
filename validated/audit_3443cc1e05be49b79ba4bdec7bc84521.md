### Title
RBF Conflicting Transactions Permanently Evicted Without Valid Replacement Due to Missing Pre-Check in `submit_entry` - (File: `tx-pool/src/process.rs`)

---

### Summary

In `TxPoolService::submit_entry`, conflicting transactions are unconditionally removed from the pool by `process_rbf` before the pool-size limit is enforced by `limit_size`. If `limit_size` then evicts the newly-inserted replacement transaction (because it has the lowest fee rate in the pool), the function returns `Err(Reject::Full(...))` — but the conflicting transactions have already been permanently removed. Both the original transactions and the replacement are absent from the pool, with no automatic recovery path.

---

### Finding Description

`submit_entry` in `tx-pool/src/process.rs` executes the following sequence under the write lock: [1](#0-0) 

**Step 1 — irreversible state mutation:** `process_rbf` removes every conflicting transaction (and its descendants) from `pool_map`, fires their `call_reject` callbacks, and moves them into the bounded `conflicts_cache` LRU: [2](#0-1) 

**Step 2 — new entry inserted:** `_submit_entry` adds the replacement transaction to the pool. [3](#0-2) 

**Step 3 — pool-size enforcement (may fail):** `limit_size` iterates the pool and evicts the entry with the lowest fee rate until `total_tx_size ≤ max_tx_pool_size`. If the newly-inserted replacement transaction is that lowest-fee-rate entry, `limit_size` returns `Some(Reject::Full(...))`, which is converted to `Err` and propagated: [4](#0-3) [5](#0-4) 

The missing precondition: the pool-size check is never performed **before** `process_rbf` removes the conflicting transactions. RBF Rule #4 (`check_rbf`) only verifies that the replacement fee exceeds `sum(replaced_fees) + extra_rbf_fee`: [6](#0-5) 

It does **not** verify that the replacement transaction's fee *rate* (fee / size) is high enough to survive `limit_size`. A large replacement transaction can satisfy the absolute-fee requirement while having a lower fee rate than every other entry in the pool.

---

### Impact Explanation

When the scenario triggers:

1. The conflicting transactions are permanently removed from `pool_map` and placed in `conflicts_cache` (an LRU cache capped at `CONFLICTES_CACHE_SIZE = 10_000`).
2. The replacement transaction is also evicted.
3. Neither is automatically re-submitted to the pool. [7](#0-6) 

The original transaction senders lose their pending transactions from the pool without any replacement being committed. Their UTXOs remain unspent on-chain but their in-flight transactions are silently dropped. The `conflicts_cache` entries age out of the LRU and are never re-broadcast. This constitutes a **tx-pool state inconsistency** reachable by any RPC caller or relay peer when RBF is enabled.

---

### Likelihood Explanation

- RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a supported and documented configuration.
- The pool must be at or near `max_tx_pool_size`.
- The attacker crafts a replacement transaction with a high absolute fee (satisfying Rule #4) but a large byte size, yielding a fee rate lower than the current pool minimum.
- These conditions are achievable by any unprivileged RPC caller (`send_transaction`) or relay peer without any special privilege.

---

### Recommendation

Perform the pool-size feasibility check **before** calling `process_rbf`. Specifically, after `_submit_entry` succeeds in a dry-run or by pre-checking the replacement transaction's fee rate against the pool's current eviction threshold, only then proceed to remove conflicting transactions. Alternatively, if `limit_size` evicts the newly-inserted replacement transaction, restore the conflicting transactions to the pool rather than leaving the pool in a partially-modified state.

---

### Proof of Concept

1. Configure a CKB node with RBF enabled (`min_rbf_rate > min_fee_rate`) and a small `max_tx_pool_size`.
2. Fill the pool to capacity with medium-fee-rate transactions.
3. Submit a transaction `T1` (the future conflict target) with a medium fee rate.
4. Craft replacement transaction `T2` that:
   - Spends the same input as `T1` (triggering RBF conflict detection).
   - Has an absolute fee exceeding `T1.fee + extra_rbf_fee` (passes `check_rbf` Rule #4).
   - Has a very large witness/output data, making its fee rate lower than every entry currently in the pool.
5. Submit `T2` via `send_transaction` RPC.
6. Observe: `submit_entry` calls `process_rbf` → `T1` is removed from `pool_map` and placed in `conflicts_cache`; `_submit_entry` inserts `T2`; `limit_size` evicts `T2` (lowest fee rate); `submit_entry` returns `Err(Reject::Full(...))`.
7. Query the pool: `T1` is absent (not in pending/proposed), `T2` is absent. Both transactions are lost from the pool. [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L96-170)
```rust
    pub(crate) async fn submit_entry(
        &self,
        pre_resolve_tip: Byte32,
        entry: TxEntry,
        mut status: TxStatus,
    ) -> (Result<(), Reject>, Arc<Snapshot>) {
        let (ret, snapshot) = self
            .with_tx_pool_write_lock(move |tx_pool, snapshot| {
                // check_rbf must be invoked in `write` lock to avoid concurrent issues.
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };

                // if snapshot changed by context switch we need redo time_relative verify
                let tip_hash = snapshot.tip_hash();
                if pre_resolve_tip != tip_hash {
                    debug!(
                        "submit_entry {} context changed. previous:{} now:{}",
                        entry.proposal_short_id(),
                        pre_resolve_tip,
                        tip_hash
                    );

                    // destructuring assignments are not currently supported
                    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

                    let tip_header = snapshot.tip_header();
                    let tx_env = status.with_env(tip_header);
                    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
                }

                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

                if !may_recovered_txs.is_empty() {
                    let self_clone = self.clone();
                    tokio::spawn(async move {
                        // push the recovered txs back to verify queue, so that they can be verified and submitted again
                        let mut queue = self_clone.verify_queue.write().await;
                        for tx in may_recovered_txs {
                            debug!("recover back: {:?}", tx.proposal_short_id());
                            let _ = queue.add_tx(tx, false, None);
                        }
                    });
                }
                Ok(())
            })
            .await;

        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L203-231)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
```

**File:** tx-pool/src/pool.rs (L30-32)
```rust
const COMMITTED_HASH_CACHE_SIZE: usize = 100_000;
const CONFLICTES_CACHE_SIZE: usize = 10_000;
const CONFLICTES_INPUTS_CACHE_SIZE: usize = 30_000;
```

**File:** tx-pool/src/pool.rs (L292-328)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

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
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
```

**File:** tx-pool/src/pool.rs (L662-676)
```rust
        // Rule #4, new tx's fee need to higher than min_rbf_fee computed from the tx_pool configuration
        // Rule #3, new tx's fee need to higher than conflicts, here we only check the all conflicted txs fee
        let fee = entry.fee;
        if let Some(min_replace_fee) = self.calculate_min_replace_fee(&all_conflicted, entry.size) {
            if fee < min_replace_fee {
                return Err(Reject::RBFRejected(format!(
                    "Tx's current fee is {}, expect it to >= {} to replace old txs",
                    fee, min_replace_fee,
                )));
            }
        } else {
            return Err(Reject::RBFRejected(
                "calculate_min_replace_fee failed".to_string(),
            ));
        }
```
