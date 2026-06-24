Audit Report

## Title
Indexer Pool `dead_cells` HashSet Loses Reference Count During RBF, Incorrectly Marking Consumed Cells as Live — (`util/indexer-sync/src/pool.rs`)

## Summary

`Pool.dead_cells` is a `HashSet<OutPoint>` with no reference counting. During RBF, both the reject notification for the old transaction and the pending notification for the replacement are dispatched asynchronously through separate channels. When the indexer processes `transaction_rejected(A)` after `new_transaction(B)`, `HashSet::remove` unconditionally deletes the shared input from `dead_cells`, causing the indexer to report that cell as live even though the replacement transaction still consumes it in the pool.

## Finding Description

`Pool.dead_cells` is declared as `HashSet<OutPoint>`: [1](#0-0) 

`new_transaction` inserts with `HashSet::insert` (idempotent — no reference count increment): [2](#0-1) 

`transaction_rejected` removes with `HashSet::remove` (unconditional — no reference count check): [3](#0-2) 

During RBF, `process_rbf` calls `callbacks.call_reject` for the old transaction, then `_submit_entry` calls `callbacks.call_pending` for the replacement — both within the same write lock: [4](#0-3) [5](#0-4) 

Both callbacks call `notify_reject_transaction` and `notify_new_transaction` respectively, each of which uses `self.handle.spawn(async move { ... })` to enqueue the message asynchronously: [6](#0-5) [7](#0-6) 

The `NotifyService` dispatches these to subscriber channels via further `self.handle.spawn` calls: [8](#0-7) [9](#0-8) 

The indexer's `index_tx_pool` loop consumes both channels via `tokio::select!`, which picks whichever branch is ready — ordering is non-deterministic: [10](#0-9) 

**Vulnerable sequence:**
1. Tx A enters pool → `new_transaction(A)` → cell `C` added to `dead_cells`.
2. Tx B (RBF, same input `C`) submitted → `process_rbf` fires `call_reject(A)`, then `_submit_entry` fires `call_pending(B)`.
3. Both notifications are spawned asynchronously. If `new_transaction(B)` is processed first: `C` is already in `dead_cells` (no-op insert).
4. `transaction_rejected(A)` is then processed: `HashSet::remove` unconditionally removes `C`.
5. `C` is now absent from `dead_cells` even though B (still in pool) consumes it.

The `dead_cells` set is used by `get_cells` to filter out pool-consumed cells from live-cell query results: [11](#0-10) 

## Impact Explanation

After RBF, the indexer incorrectly reports cells consumed by the replacement transaction as live. Wallets and dApps querying `get_cells` or `get_cells_capacity` will see those cells as spendable, build transactions on them, and receive double-spend rejections from the node. This is a concrete incorrect implementation of the CKB state storage mechanism (the indexer's pool-overlay state), matching **Medium (2001–10000 points): Suboptimal/incorrect implementation of CKB state storage mechanism**.

## Likelihood Explanation

RBF is a supported, documented feature enabled via `min_rbf_rate > 0`. Any unprivileged user can trigger this by submitting a transaction and then submitting a higher-fee replacement via `send_transaction` RPC. The async notification ordering is non-deterministic under normal Tokio scheduling — multiple levels of `tokio::spawn` and `tokio::select!` across separate channels make the race practically reachable without special timing. No privileged access is required.

## Recommendation

Replace `dead_cells: HashSet<OutPoint>` with `HashMap<OutPoint, usize>`. Increment the count in `new_transaction` and decrement (removing the entry only when the count reaches zero) in both `transaction_rejected` and `transaction_committed`. This ensures a cell is only removed from `dead_cells` when no remaining pool transaction references it.

## Proof of Concept

1. Configure node with `min_rbf_rate > 0` and `index_tx_pool = true`.
2. Submit Tx A spending cell `C` via `send_transaction`. Verify `get_cells` excludes `C`.
3. Submit Tx B (higher fee, same input `C`) via `send_transaction`. RBF fires.
4. Immediately query `get_cells` for the lock script owning `C`. Under the race condition, `C` appears as a live cell.
5. Attempt to submit a new transaction spending `C` — the node rejects it as a double-spend, confirming the indexer's state is incorrect while B remains in the pool.
6. Reproducible as a unit test by mocking the notification channels and delivering `new_transaction(B)` before `transaction_rejected(A)` to the indexer's pool.

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

**File:** util/indexer-sync/src/pool.rs (L123-143)
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
                    _ = stop.cancelled() => {
                        info!("index_tx_pool received exit signal, exit now");
                        break
                    },
                    else => break,
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

**File:** notify/src/lib.rs (L315-328)
```rust
    fn handle_notify_new_transaction(&self, tx_entry: PoolTransactionEntry) {
        trace!("New tx event {:?}", tx_entry);
        // notify all subscribers
        let tx_timeout = self.timeout.tx;
        // notify all subscribers
        for subscriber in self.new_transaction_subscribers.values() {
            let tx_entry = tx_entry.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(tx_entry, tx_timeout).await {
                    error!("Failed to notify new transaction, error: {}", e);
                }
            });
        }
```

**File:** notify/src/lib.rs (L375-388)
```rust
    fn handle_notify_reject_transaction(&self, tx_entry: (PoolTransactionEntry, Reject)) {
        trace!("Tx reject event {:?}", tx_entry);
        // notify all subscribers
        let tx_timeout = self.timeout.tx;
        // notify all subscribers
        for subscriber in self.reject_transaction_subscribers.values() {
            let tx_entry = tx_entry.clone();
            let subscriber = subscriber.clone();
            self.handle.spawn(async move {
                if let Err(e) = subscriber.send_timeout(tx_entry, tx_timeout).await {
                    error!("Failed to notify transaction reject, error: {}", e);
                }
            });
        }
```

**File:** notify/src/lib.rs (L505-511)
```rust
    pub fn notify_new_transaction(&self, tx_entry: PoolTransactionEntry) {
        let new_transaction_notifier = self.new_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = new_transaction_notifier.send(tx_entry).await {
                error!("notify_new_transaction channel is closed: {}", e);
            }
        });
```

**File:** notify/src/lib.rs (L549-555)
```rust
    pub fn notify_reject_transaction(&self, tx_entry: PoolTransactionEntry, reject: Reject) {
        let reject_transaction_notifier = self.reject_transaction_notifier.clone();
        self.handle.spawn(async move {
            if let Err(e) = reject_transaction_notifier.send((tx_entry, reject)).await {
                error!("notify_reject_transaction channel is closed: {}", e);
            }
        });
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L109-135)
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
        }
```
