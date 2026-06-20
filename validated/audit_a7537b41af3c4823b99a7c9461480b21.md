### Title
Missing Reject Notification When `remove_tx` Silently Drops Transactions and Descendants from the Pool - (File: `tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

---

### Summary

`TxPool::remove_tx` and `TxPoolService::remove_tx` silently remove transactions (and all their descendants) from the pool without invoking any reject callback. Every other removal path in the tx-pool — expiry, size-limit eviction, RBF replacement, committed-tx conflict — correctly calls `callbacks.call_reject`, which triggers the `reject_transaction` notification to all subscribers. The `remove_tx` path is the sole exception. The indexer pool overlay (`util/indexer-sync/src/pool.rs`) relies exclusively on `reject_transaction` events to remove dead-cell entries it added on `new_transaction`. When `remove_tx` is used, those dead-cell entries are never cleaned up, leaving the indexer with permanently stale state.

---

### Finding Description

The CKB tx-pool exposes a callback/notification system for three lifecycle events: a transaction entering the pending pool (`call_pending`), entering the proposed pool (`call_proposed`), and being removed for any reason (`call_reject`). The `call_reject` path is wired in `shared/src/shared_builder.rs` to emit a `notify_reject_transaction` event to all subscribers.

Every internal removal path correctly fires `call_reject`:

- `remove_expired` — calls `callbacks.call_reject(self, &entry, reject)` for each expired entry. [1](#0-0) 
- `limit_size` — calls `callbacks.call_reject(self, &entry, reject)` for each evicted entry. [2](#0-1) 
- `process_rbf` — calls `self.callbacks.call_reject(tx_pool, &old, reject)` for each replaced entry. [3](#0-2) 
- `remove_committed_tx` — calls `callbacks.call_reject` for conflicting entries. [4](#0-3) 

`TxPool::remove_tx`, however, simply calls `pool_map.remove_entry_and_descendants` and returns a boolean — no callback, no notification:

```rust
pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
    let entries = self.pool_map.remove_entry_and_descendants(id);
    !entries.is_empty()
}
``` [5](#0-4) 

`TxPoolService::remove_tx` calls this after also silently dropping entries from the verify queue and orphan pool — again with no notification at any stage:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    { let mut queue = self.verify_queue.write().await;
      if queue.remove_tx(&id).is_some() { return true; } }
    { let mut orphan = self.orphan.write().await;
      if orphan.remove_orphan_tx(&id).is_some() { return true; } }
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.remove_tx(&id)
}
``` [6](#0-5) 

The indexer pool overlay subscribes to exactly two events — `new_transaction` and `reject_transaction` — to maintain its dead-cell set:

- On `new_transaction`: marks all inputs of the accepted tx as dead cells. [7](#0-6) 
- On `reject_transaction`: removes those inputs from the dead-cell set. [8](#0-7) 

When a transaction is accepted into the pending pool (triggering `new_transaction`), its inputs are recorded as dead cells. If that transaction is later removed via `remove_tx` — which emits no `reject_transaction` — the indexer never cleans up those entries. The cells remain permanently marked as consumed in the indexer's overlay. [9](#0-8) 

The same silent-removal gap applies to all descendants removed by `remove_entry_and_descendants` inside `remove_tx`. [10](#0-9) 

---

### Impact Explanation

Any off-chain client or DApp using the indexer's pool overlay to check cell liveness will observe cells as permanently consumed (dead) after the transaction that spent them is silently dropped from the pool. A wallet querying available UTXOs will undercount spendable cells. A DApp checking whether a specific cell is available for use will receive an incorrect "consumed" answer. This is not merely observability noise — it is incorrect state that persists indefinitely until the node is restarted or the indexer is resynced, directly affecting user-facing balance and cell-availability queries.

---

### Likelihood Explanation

Any code path that invokes `TxPoolService::remove_tx` on a transaction that was previously accepted into the pending pool (and thus already recorded by the indexer via `new_transaction`) triggers this inconsistency. The tx-pool service exposes `remove_tx` as an internal command reachable from the service message loop. A transaction submitter can craft a scenario where a transaction is accepted into the pending pool (triggering `new_transaction` and dead-cell recording) and is subsequently removed via `remove_tx` without a corresponding reject notification. The gap is structural and reproducible whenever this code path is exercised.

---

### Recommendation

`TxPool::remove_tx` should call `callbacks.call_reject` for each removed entry (including descendants), using an appropriate `Reject` variant (e.g., `Reject::Resolve` or a new `Reject::Removed` variant), mirroring the pattern used in `remove_expired` and `limit_size`. `TxPoolService::remove_tx` should similarly emit reject notifications for entries removed from the verify queue and orphan pool, so all subscribers — including the indexer — receive consistent lifecycle events for every transaction state change.

---

### Proof of Concept

1. Enable the indexer with `index_tx_pool = true`.
2. Submit transaction `T` with input cell `C`. `T` is accepted into the pending pool; `new_transaction` fires; the indexer marks `C` as a dead cell.
3. Trigger `TxPoolService::remove_tx` for `T`'s hash. `T` is removed from `tx_pool` via `TxPool::remove_tx` with no callback invoked; no `reject_transaction` event fires.
4. Query the indexer for the status of cell `C`. The indexer reports `C` as consumed by a pool transaction, even though `T` no longer exists in the pool.
5. Contrast: submit a second transaction `T2` that conflicts with `T` (triggering `remove_committed_tx` or `limit_size`). In that case, `call_reject` fires, `reject_transaction` is emitted, and the indexer correctly clears `C` from its dead-cell set.

### Citations

**File:** tx-pool/src/pool.rs (L259-266)
```rust
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
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

**File:** tx-pool/src/pool.rs (L307-323)
```rust
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
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
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

**File:** tx-pool/src/process.rs (L440-456)
```rust
    pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
        let id = ProposalShortId::from_tx_hash(&tx_hash);
        {
            let mut queue = self.verify_queue.write().await;
            if queue.remove_tx(&id).is_some() {
                return true;
            }
        }
        {
            let mut orphan = self.orphan.write().await;
            if orphan.remove_orphan_tx(&id).is_some() {
                return true;
            }
        }
        let mut tx_pool = self.tx_pool.write().await;
        tx_pool.remove_tx(&id)
    }
```

**File:** util/indexer-sync/src/pool.rs (L17-62)
```rust
/// An overlay to index the pending txs in the ckb tx pool,
/// currently only supports removals of dead cells from the pending txs
#[derive(Default)]
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}

impl Pool {
    /// the tx has been committed in a block, it should be removed from pending dead cells
    pub fn transaction_committed(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// the tx has been rejected for some reason, it should be removed from pending dead cells
    pub fn transaction_rejected(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }

    /// a new tx is submitted to the pool, mark its inputs as dead cells
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
    }

    /// Return weather out_point referred cell consumed by pooled transaction
    pub fn is_consumed_by_pool_tx(&self, out_point: &OutPoint) -> bool {
        self.dead_cells.contains(out_point)
    }

    /// the txs has been committed in a block, it should be removed from pending dead cells
    pub fn transactions_committed(&mut self, txs: &[TransactionView]) {
        for tx in txs {
            self.transaction_committed(tx);
        }
    }

    /// return all dead cells
    pub fn dead_cells(&self) -> impl Iterator<Item = &OutPoint> {
        self.dead_cells.iter()
    }
}
```

**File:** util/indexer-sync/src/pool.rs (L125-128)
```rust
                    Some(tx_entry) = new_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write().expect("acquire lock").new_transaction(&tx_entry.transaction);
                        }
```

**File:** util/indexer-sync/src/pool.rs (L130-135)
```rust
                    Some((tx_entry, _reject)) = reject_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write()
                            .expect("acquire lock")
                            .transaction_rejected(&tx_entry.transaction);
                        }
```

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
    }
```
