### Title
Check-Effects-Interactions Violation in `submit_entry`: `call_pending` Fires Before `limit_size` Eviction Completes — (`File: tx-pool/src/process.rs`)

---

### Summary

Inside `TxPoolService::submit_entry`, a newly-submitted transaction is added to the pool and the `call_pending` callback fires (broadcasting a `new_transaction` notification to all subscribers and the fee estimator) **before** `limit_size` runs. If `limit_size` then evicts that same entry, `call_reject` fires a contradictory `reject_transaction` notification. This violates the Check-Effects-Interactions pattern: the external interaction (subscriber notification + relay message) precedes the final determination of pool state.

---

### Finding Description

**Root cause — ordering in `submit_entry`:**

`submit_entry` holds the tx-pool write lock and executes the following sequence:

1. `process_rbf` removes conflicting txs and calls `call_reject` on each (writing to `recent_reject` DB and sending relay messages) **before** the new tx is in the pool.
2. `_submit_entry` adds the new entry to the pool and immediately calls `call_pending`.
3. `limit_size` is called — it may evict the entry that was just added and call `call_reject` on it. [1](#0-0) 

Inside `_submit_entry`, the pool insertion and the `call_pending` callback are back-to-back with no intervening size check: [2](#0-1) 

`call_pending` triggers `notify_new_transaction`, which spawns an async task that sends the entry to all registered subscribers: [3](#0-2) 

`limit_size` then runs and may evict the same entry, calling `call_reject`: [4](#0-3) 

The registered `call_reject` callback writes to the `recent_reject` persistent DB **and** sends a `TxVerificationResult::Reject` relay message to the network: [5](#0-4) 

Additionally, in `process_rbf`, `call_reject` is fired for each RBF-evicted tx **before** the replacing tx is inserted into the pool, meaning the relay rejection message goes out while the pool is in a half-updated state (old tx gone, new tx absent): [6](#0-5) 

**Double-write to `recent_reject`:** When `limit_size` evicts the newly-added entry, `call_reject` writes to `recent_reject`. Then `submit_entry` returns `Err(Reject::Full(...))`, and the caller (`resumeble_process_tx_and_notify_full_reject`) sends another relay rejection. `after_process` may also call `put_recent_reject` for the same tx hash, resulting in a duplicate DB write. [7](#0-6) 

The `RejectCallback` type signature accepts `&mut TxPool`, meaning the callback executes with mutable pool access while the pool is in a partially-updated state: [8](#0-7) 

---

### Impact Explanation

- **Contradictory subscriber notifications:** Any external process subscribed to `new_transaction` and `reject_transaction` events (e.g., external scripts configured via `notify_config`, RPC subscribers) receives a `new_transaction` event followed by a `reject_transaction` event for the same tx hash. Downstream systems that act on `new_transaction` (e.g., updating their own accounting or triggering dependent actions) will be left in an inconsistent state.
- **Duplicate `recent_reject` DB writes:** The same tx hash is written to the `recent_reject` RocksDB-with-TTL instance twice, incrementing `total_keys_num` twice and potentially triggering an unnecessary `shrink()` operation.
- **Relay message sent for a tx that was briefly accepted:** Peers receive a `Reject` relay result for a tx that was momentarily in the pool, causing their bloom filters or known-tx sets to mark it as rejected when it was transiently valid. [9](#0-8) 

---

### Likelihood Explanation

Reachable by any unprivileged RPC caller (`send_transaction`) or P2P peer relaying a transaction. The trigger condition — pool at or near `max_tx_pool_size` — is a normal operational state on a busy node. An attacker can deliberately fill the pool with low-fee transactions and then submit a target transaction to reliably trigger the `call_pending` → `limit_size` → `call_reject` sequence. No privileged keys or special roles are required. [10](#0-9) 

---

### Recommendation

Apply the Check-Effects-Interactions pattern: determine the final pool state (including `limit_size`) **before** firing any external callbacks or notifications.

Concretely:
1. In `submit_entry`, call `limit_size` before calling `_submit_entry` (or restructure so that `call_pending` is only invoked after `limit_size` confirms the entry will remain in the pool).
2. In `process_rbf`, defer `call_reject` for RBF-evicted txs until after the replacing tx has been successfully inserted via `_submit_entry`.
3. Ensure `recent_reject` is written only once per rejection event — either in the callback or in `after_process`, not both.

---

### Proof of Concept

1. Fill the tx-pool to `max_tx_pool_size` with low-fee-rate transactions.
2. Submit a new transaction via RPC (`send_transaction`) with a fee rate just above `min_fee_rate` but below the lowest entry in the pool.
3. Observe the execution path in `submit_entry`:
   - `_submit_entry` succeeds → `call_pending` fires → `notify_new_transaction` is sent to all subscribers.
   - `limit_size` evicts the new entry (lowest fee rate) → `call_reject` fires → `notify_reject_transaction` is sent to all subscribers AND `recent_reject` is written.
   - `submit_entry` returns `Err(Reject::Full(...))`.
   - `resumeble_process_tx_and_notify_full_reject` sends a second relay rejection.
4. A subscriber (e.g., a script registered under `new_block_notify_script` or an RPC `subscribe` client) receives both `new_transaction` and `reject_transaction` for the same tx hash in the same processing cycle, demonstrating the inconsistent interaction-before-effects ordering. [11](#0-10) [12](#0-11)

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

**File:** tx-pool/src/process.rs (L219-231)
```rust
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

**File:** tx-pool/src/process.rs (L355-368)
```rust
    async fn resumeble_process_tx_and_notify_full_reject(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let tx_hash = tx.hash();
        let ret = self.resumeble_process_tx(tx, is_proposal_tx, remote).await;

        if matches!(ret, Err(Reject::Full(_))) {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }

        ret
```

**File:** tx-pool/src/process.rs (L1016-1037)
```rust
fn _submit_entry(
    tx_pool: &mut TxPool,
    status: TxStatus,
    entry: TxEntry,
    callbacks: &Callbacks,
) -> Result<HashSet<TxEntry>, Reject> {
    let tx_hash = entry.transaction().hash();
    debug!("submit_entry {:?} {}", status, tx_hash);
    let (succ, evicts) = match status {
        TxStatus::Fresh => tx_pool.add_pending(entry.clone())?,
        TxStatus::Gap => tx_pool.add_gap(entry.clone())?,
        TxStatus::Proposed => tx_pool.add_proposed(entry.clone())?,
    };
    if succ {
        match status {
            TxStatus::Fresh => callbacks.call_pending(&entry),
            TxStatus::Gap => callbacks.call_pending(&entry),
            TxStatus::Proposed => callbacks.call_proposed(&entry),
        }
    }
    Ok(evicts)
}
```

**File:** shared/src/shared_builder.rs (L559-566)
```rust
    tx_pool_builder.register_pending(Box::new(move |entry: &TxEntry| {
        // notify
        let notify_tx_entry = create_notify_entry(entry);
        notify_pending.notify_new_transaction(notify_tx_entry);
        let tx_hash = entry.transaction().hash();
        let entry_info = entry.to_info();
        fee_estimator_clone.accept_tx(tx_hash, entry_info);
    }));
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

**File:** tx-pool/src/pool.rs (L292-298)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
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

**File:** tx-pool/src/callback.rs (L10-10)
```rust
pub type RejectCallback = Box<dyn Fn(&mut TxPool, &TxEntry, Reject) + Sync + Send>;
```

**File:** tx-pool/src/component/recent_reject.rs (L55-70)
```rust
    pub fn put(&mut self, hash: &Byte32, reject: Reject) -> Result<(), AnyError> {
        let hash_slice = hash.as_slice();
        let shard = self.get_shard(hash_slice).to_string();
        let reject: ckb_jsonrpc_types::PoolTransactionReject = reject.into();
        let json_string = serde_json::to_string(&reject)?;
        self.db.put(&shard, hash_slice, json_string)?;

        if let Some(total_keys_num) = self.total_keys_num.checked_add(1) {
            if total_keys_num > self.count_limit {
                self.shrink()?;
            }
        } else {
            // overflow occurred, try shrink
            self.shrink()?;
        }
        Ok(())
```
