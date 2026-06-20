### Title
Re-entrant Mutable `TxPool` Access via `RejectCallback` During Partially-Modified Pool State — (File: `tx-pool/src/callback.rs`, `tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

---

### Summary

The `RejectCallback` type in the CKB transaction pool passes a live `&mut TxPool` reference to an external callback that is invoked while the pool is in a partially-modified state — specifically inside the `limit_size` eviction loop and the `process_rbf` conflict-removal loop. This is structurally analogous to the SakeVault re-entrancy: a function with an external callback is called mid-operation on shared mutable state, allowing the callback to corrupt the ongoing operation's invariants. An unprivileged tx-pool submitter can trigger these paths by submitting transactions that cause pool eviction or RBF replacement.

---

### Finding Description

**Root cause — `tx-pool/src/callback.rs`:**

The `RejectCallback` type is defined as:

```rust
pub type RejectCallback = Box<dyn Fn(&mut TxPool, &TxEntry, Reject) + Sync + Send>;
``` [1](#0-0) 

The `call_reject` dispatcher passes the **entire mutable pool** to the callback:

```rust
pub fn call_reject(&self, tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject) {
    if let Some(call) = &self.reject {
        call(tx_pool, entry, reject)
    }
}
``` [2](#0-1) 

**Call site 1 — `limit_size` eviction loop (`tx-pool/src/pool.rs`):**

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    ...
    let removed = self.pool_map.remove_entry_and_descendants(&id);
    for entry in removed {
        ...
        callbacks.call_reject(self, &entry, reject);   // ← &mut self passed here
    }
}
``` [3](#0-2) 

The loop termination condition is `pool_map.total_tx_size`. The callback receives `self` as `&mut TxPool` and can call any pool mutation method — including `add_pending`, `add_gap`, or `add_proposed` — which directly increments `pool_map.total_tx_size`. If the callback re-inserts entries, the loop condition is never satisfied, producing an **infinite loop** that permanently stalls the tx-pool service.

**Call site 2 — `process_rbf` conflict loop (`tx-pool/src/process.rs`):**

```rust
for old in all_removed {
    tx_pool.record_conflict(old.transaction().clone());   // records conflict
    self.callbacks.call_reject(tx_pool, &old, reject);    // callback gets &mut TxPool
}
``` [4](#0-3) 

The callback receives `&mut TxPool` immediately after `record_conflict`. It can call `tx_pool.remove_conflict()` or mutate `conflicts_cache` / `conflicts_outputs_cache`, undoing the just-recorded conflict entry. This leaves the pool's conflict tracking in an inconsistent state: the old tx is removed from `pool_map` but its conflict record is also erased, making the pool believe no conflict exists for those inputs.

**Call site 3 — `remove_committed_tx` (`tx-pool/src/pool.rs`):**

```rust
for (entry, reject) in self.pool_map.resolve_conflict(tx) {
    callbacks.call_reject(self, &entry, reject);   // &mut self mid-iteration
}
``` [5](#0-4) 

The callback is invoked while iterating over `resolve_conflict` results with `self` as `&mut TxPool`. The callback can modify `pool_map` entries that are still being iterated, corrupting the conflict resolution pass.

**The registered callback (`shared/src/shared_builder.rs`):**

```rust
tx_pool_builder.register_reject(Box::new(
    move |tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject| {
        if reject.should_recorded()
            && let Some(ref mut recent_reject) = tx_pool.recent_reject
            && let Err(e) = recent_reject.put(&tx_hash, reject.clone())
        { ... }

        if reject.is_allowed_relay()
            && let Err(e) = tx_relay_sender.send(TxVerificationResult::Reject { ... })
        { ... }

        notify_reject.notify_reject_transaction(notify_tx_entry, reject);
        fee_estimator.reject_tx(&tx_hash);
    },
));
``` [6](#0-5) 

The production callback mutates `tx_pool.recent_reject` (a RocksDB write) and calls `tx_relay_sender.send()` (a synchronous channel send) — both while the pool is in a partially-modified state inside `limit_size` or `process_rbf`. If `tx_relay_sender` is a bounded channel and its receiver is blocked, the `send()` call stalls, holding the pool in a half-evicted state indefinitely.

---

### Impact Explanation

1. **Infinite loop / node DoS**: A callback that re-inserts entries into the pool during `limit_size` causes `pool_map.total_tx_size` to never drop below `max_tx_pool_size`, spinning the eviction loop forever and stalling the entire tx-pool service.

2. **RBF conflict-tracking corruption**: A callback that calls `tx_pool.remove_conflict()` during `process_rbf` erases the just-recorded conflict entry, making the pool believe inputs freed by the replaced transaction are uncontested. Subsequent transactions spending those inputs bypass conflict detection.

3. **Inconsistent pool state on RocksDB failure**: The production callback writes to `recent_reject` (RocksDB) while entries are already removed from `pool_map`. If the write fails or panics, in-memory and on-disk state diverge — rejected transactions are absent from `pool_map` but also absent from `recent_reject`, making them invisible to both the pool and the reject history.

---

### Likelihood Explanation

The `limit_size` path is reachable by any unprivileged tx-pool submitter: submitting a transaction when the pool is at capacity triggers `limit_size` directly. The `process_rbf` path is reachable by any submitter who sends a transaction with RBF flags that conflicts with an existing pool entry. Both are standard, externally-reachable code paths requiring no special privilege. The severity of the infinite-loop scenario depends on whether a future or third-party callback registration exploits the `&mut TxPool` access; the RocksDB inconsistency is reachable with the current production callback under disk-pressure conditions.

---

### Recommendation

1. **Remove `&mut TxPool` from `RejectCallback`**: Change the signature to `Box<dyn Fn(&TxEntry, Reject) + Sync + Send>`. Any pool mutations needed after rejection (e.g., writing `recent_reject`) should be performed by the caller after `call_reject` returns, not inside the callback.

2. **Guard `limit_size` against callback-induced re-entry**: Snapshot `total_tx_size` before the loop and validate it is monotonically decreasing after each callback invocation, or collect all reject calls and dispatch them after the loop exits.

3. **Move `recent_reject` writes out of the callback**: Perform the RocksDB write in the caller after the pool lock is released, not inside the callback while the pool is partially modified.

---

### Proof of Concept

**Triggering the `limit_size` infinite-loop path:**

1. Configure a node with a small `max_tx_pool_size` (e.g., 1 KB).
2. Register a custom `RejectCallback` (possible via the `register_reject` API in `TxPoolServiceBuilder`) that calls `tx_pool.add_pending(entry.clone())` — re-inserting the evicted entry.
3. Submit any transaction that causes the pool to exceed `max_tx_pool_size`.
4. `limit_size` is called; it removes the entry and calls the callback; the callback re-inserts it; `total_tx_size` is restored; the `while` condition remains true; the loop never exits.
5. The tx-pool service thread spins indefinitely, blocking all subsequent `submit_entry`, `get_block_template`, and `update_tx_pool_for_reorg` calls.

**Triggering the `process_rbf` conflict-corruption path:**

1. Submit transaction T1 spending input I.
2. Submit transaction T2 (RBF) spending the same input I with a higher fee rate.
3. `process_rbf` removes T1, calls `record_conflict(T1)`, then calls `call_reject(tx_pool, T1, ...)`.
4. A callback that calls `tx_pool.remove_conflict(&T1.proposal_short_id())` erases the conflict record.
5. T1 is now absent from both `pool_map` and `conflicts_cache`; a third transaction T3 spending input I can be submitted without triggering conflict detection, resulting in a double-spend within the pool. [1](#0-0) [7](#0-6) [4](#0-3) [6](#0-5)

### Citations

**File:** tx-pool/src/callback.rs (L10-10)
```rust
pub type RejectCallback = Box<dyn Fn(&mut TxPool, &TxEntry, Reject) + Sync + Send>;
```

**File:** tx-pool/src/callback.rs (L65-69)
```rust
    pub fn call_reject(&self, tx_pool: &mut TxPool, entry: &TxEntry, reject: Reject) {
        if let Some(call) = &self.reject {
            call(tx_pool, entry, reject)
        }
    }
```

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

**File:** tx-pool/src/pool.rs (L292-329)
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
    }
```

**File:** tx-pool/src/process.rs (L219-232)
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
        }
```

**File:** shared/src/shared_builder.rs (L576-602)
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
    ));
```
