### Title
Indexer Pool `dead_cells` Uses Non-Reference-Counted `HashSet`, Allowing RBF Replacement to Silently Unlock Consumed Cells — (`util/indexer-sync/src/pool.rs`)

---

### Summary

The `Pool` struct in `util/indexer-sync/src/pool.rs` tracks cells consumed by pending transactions using a `HashSet<OutPoint>`. Because a `HashSet` stores at most one entry per key regardless of how many transactions reference it, an RBF (Replace-By-Fee) replacement causes the replaced transaction's inputs — including those shared with the replacement — to be unconditionally removed from `dead_cells`. The replacement transaction's inputs are then no longer marked as consumed, causing the indexer to incorrectly report those cells as live even though they are still consumed by the replacement transaction in the pool.

---

### Finding Description

`Pool.dead_cells` is a `HashSet<OutPoint>`: [1](#0-0) 

`new_transaction` inserts inputs with `HashSet::insert` (idempotent — no reference count): [2](#0-1) 

`transaction_rejected` removes inputs with `HashSet::remove` (unconditional — no reference count check): [3](#0-2) 

During RBF, `process_rbf` in `tx-pool/src/process.rs` removes the old conflicting transaction from the pool and calls the reject callback, then `_submit_entry` adds the new transaction and calls the pending callback: [4](#0-3) [5](#0-4) 

Both notifications are dispatched asynchronously via `tokio::spawn` through separate channels: [6](#0-5) [7](#0-6) 

The indexer's `index_tx_pool` loop processes these events via `tokio::select!`, which picks whichever channel has a message ready — the order is non-deterministic: [8](#0-7) 

**Vulnerable sequence (RBF with shared inputs):**

1. Tx A enters pool → `new_transaction(A)` → A's inputs added to `dead_cells`.
2. Tx B (RBF replacement, sharing inputs with A) is submitted.
3. `new_transaction(B)` fires → B's shared inputs are already in `dead_cells` (no-op insert).
4. `transaction_rejected(A)` fires → `HashSet::remove` unconditionally removes ALL of A's inputs, including those shared with B.
5. B's shared inputs are now absent from `dead_cells`, even though B is still in the pool consuming them.

This is the direct analog of the Flatcoin bug: two "locks" (A and B both referencing the same cell) are applied, but canceling one (rejecting A) removes the lock entirely, bypassing the protection that was supposed to remain active for B.

The `dead_cells` set is consumed by the rich indexer's `get_cells` query to filter out pool-consumed cells: [9](#0-8) 

---

### Impact Explanation

After an RBF replacement, the indexer incorrectly reports cells consumed by the replacement transaction as "live." Wallets and dApps querying `get_cells` or `get_cells_capacity` will see those cells as available for spending. Any transaction built on this incorrect data will be rejected by the node (double-spend), causing failed transactions and incorrect balance displays. The indexer's cell-status invariant — that cells consumed by pending pool transactions are excluded from live-cell results — is silently violated.

---

### Likelihood Explanation

RBF is a supported, documented feature enabled via `min_rbf_rate` configuration. Any unprivileged user can trigger this by submitting a transaction and then submitting a higher-fee replacement via the `send_transaction` RPC. The async notification ordering is non-deterministic under normal load, making the race condition practically reachable without any special timing. No privileged access is required.

---

### Recommendation

Replace `dead_cells: HashSet<OutPoint>` with a reference-counted map `HashMap<OutPoint, usize>`. Increment the count in `new_transaction` and decrement (removing the entry only when the count reaches zero) in `transaction_rejected` and `transaction_committed`. This mirrors the correct fix for the Flatcoin bug: track how many active references hold each lock, and only release the lock when all references are gone.

```rust
pub struct Pool {
    dead_cells: HashMap<OutPoint, usize>,
}

pub fn new_transaction(&mut self, tx: &TransactionView) {
    for input in tx.inputs() {
        *self.dead_cells.entry(input.previous_output()).or_insert(0) += 1;
    }
}

pub fn transaction_rejected(&mut self, tx: &TransactionView) {
    for input in tx.inputs() {
        let op = input.previous_output();
        if let Entry::Occupied(mut e) = self.dead_cells.entry(op) {
            *e.get_mut() -= 1;
            if *e.get() == 0 { e.remove(); }
        }
    }
}
```

---

### Proof of Concept

1. Enable RBF (`min_rbf_rate > 0`) and enable indexer tx-pool tracking (`index_tx_pool = true`).
2. Submit Tx A spending cell `C` via `send_transaction`. Indexer marks `C` as dead.
3. Submit Tx B (higher fee, same input `C`) via `send_transaction`. RBF fires: `new_transaction(B)` and `transaction_rejected(A)` are dispatched asynchronously.
4. When `transaction_rejected(A)` is processed after `new_transaction(B)`, `C` is removed from `dead_cells`.
5. Query `get_cells` for the lock script owning `C`. The indexer returns `C` as a live cell, even though B (still in the pool) consumes it.
6. Build and submit a new transaction spending `C` — it will be rejected by the node as a double-spend, confirming the indexer's state is incorrect.

### Citations

**File:** util/indexer-sync/src/pool.rs (L20-22)
```rust
pub struct Pool {
    dead_cells: HashSet<OutPoint>,
}
```

**File:** util/indexer-sync/src/pool.rs (L32-37)
```rust
    /// the tx has been rejected for some reason, it should be removed from pending dead cells
    pub fn transaction_rejected(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.remove(&input.previous_output());
        }
    }
```

**File:** util/indexer-sync/src/pool.rs (L39-44)
```rust
    /// a new tx is submitted to the pool, mark its inputs as dead cells
    pub fn new_transaction(&mut self, tx: &TransactionView) {
        for input in tx.inputs() {
            self.dead_cells.insert(input.previous_output());
        }
    }
```

**File:** util/indexer-sync/src/pool.rs (L123-136)
```rust
            loop {
                tokio::select! {
                    Some(tx_entry) = new_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write().expect("acquire lock").new_transaction(&tx_entry.transaction);
                        }
                    }
                    Some((tx_entry, _reject)) = reject_transaction_receiver.recv() => {
                        if let Some(pool) = service.pool.as_ref() {
                            pool.write()
                            .expect("acquire lock")
                            .transaction_rejected(&tx_entry.transaction);
                        }
                    }
```

**File:** tx-pool/src/process.rs (L136-137)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L109-134)
```rust
        // filter cells in pool
        let mut dead_cells = Vec::new();
        if let Some(pool) = self
            .pool
            .as_ref()
            .map(|pool| pool.read().expect("acquire lock"))
        {
            dead_cells = pool
                .dead_cells()
                .map(|out_point| {
                    let tx_hash: H256 = out_point.tx_hash().into();
                    (tx_hash.as_bytes().to_vec(), out_point.index().into())
                })
                .collect::<Vec<(_, u32)>>()
        }
        if !dead_cells.is_empty() {
            let placeholders = dead_cells
                .iter()
                .map(|(_, output_index)| {
                    let placeholder = format!("(${}, {})", param_index, output_index);
                    param_index += 1;
                    placeholder
                })
                .collect::<Vec<_>>()
                .join(",");
            query_builder.and_where(format!("(tx_hash, output_index) NOT IN ({})", placeholders));
```
