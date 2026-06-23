### Title
`remove_tx` RPC Fails to Cancel In-Flight Transactions Being Verified by Worker — (`tx-pool/src/process.rs`)

### Summary

`TxPoolService::remove_tx` checks three disjoint stores (`verify_queue`, `orphan`, `tx_pool`) sequentially to remove a transaction. However, there is an unguarded intermediate state: a transaction that has been **popped from `verify_queue` by a verify worker but not yet submitted to `tx_pool`** is invisible to all three checks. The worker then completes verification and unconditionally submits the transaction to `tx_pool`, making the removal ineffective. This is a direct structural analog to the Vader `cancelProposal` bug: a cancellation operation only clears one piece of state while leaving residual execution state intact.

---

### Finding Description

The `remove_tx` function in `tx-pool/src/process.rs` is the handler for the `remove_transaction` RPC:

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
``` [1](#0-0) 

Concurrently, the verify worker in `verify_mgr.rs` operates as follows:

```rust
// Step 1: pop the entry — releases the write lock immediately after
let entry = {
    let mut tasks = self.tasks.write().await;
    match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
        Some(entry) => entry,
        None => { ... return; }
    }
};
// Lock is released here. The tx is now in NEITHER verify_queue NOR tx_pool.

// Step 2: verify (can be long for complex scripts)
if let Some((res, snapshot)) = self.service._process_tx(
    entry.tx.clone(), entry.remote.map(|e| e.0), Some(&mut self.command_rx),
).await {
    // Step 3: submit to tx_pool unconditionally
    self.service.after_process(entry.tx, entry.remote, &snapshot, &res).await;
}
``` [2](#0-1) 

Between Step 1 (pop from queue) and Step 3 (submit to pool), the transaction is **invisible to all three stores** checked by `remove_tx`. If `remove_tx` is called during this window:

1. `verify_queue.remove_tx(&id)` → `None` (already popped)
2. `orphan.remove_orphan_tx(&id)` → `None` (never there)
3. `tx_pool.remove_tx(&id)` → `false` (not yet submitted)

`remove_tx` returns `false` ("not found"), but the worker proceeds to call `submit_entry` → `_submit_entry` → `add_pending`/`add_proposed`, inserting the transaction into the pool. [3](#0-2) [4](#0-3) 

There is no "cancelled" flag, no tombstone set, and no check in `submit_entry` for whether the transaction was removed while it was being verified. [5](#0-4) 

---

### Impact Explanation

A node operator invoking the `remove_transaction` RPC to evict a transaction from the pool cannot reliably do so if the transaction is currently undergoing script verification. The removal silently fails (`false` returned), and the transaction is subsequently inserted into `tx_pool` in `Pending`, `Gap`, or `Proposed` status. The transaction will then be eligible for inclusion in a block template, defeating the operator's intent. For transactions with complex scripts (high-cycle), the verification window is long, making the race window wide and practically exploitable.

---

### Likelihood Explanation

The race window exists for every transaction that enters the async verify pipeline. For transactions with large declared cycles (up to `max_tx_verify_cycles`), the `verify_rtx` call inside `_process_tx` can take hundreds of milliseconds. Any `remove_transaction` RPC call during that window will silently fail. An RPC caller who submits a high-cycle transaction and immediately calls `remove_transaction` will reliably trigger this condition. No special privileges beyond standard RPC access are required. [6](#0-5) 

---

### Recommendation

Introduce a `pending_removal: HashSet<ProposalShortId>` set (protected by the same `RwLock` as `verify_queue`, or a dedicated `Mutex`) in `TxPoolService`. In `remove_tx`, after failing to find the transaction in all three stores, insert its `ProposalShortId` into this set. In `submit_entry` (or `_submit_entry`), check this set before inserting into the pool and abort if the id is present, then remove it from the set. This mirrors the recommended fix in the Vader report: set a cancel flag and check it at the execution stage.

---

### Proof of Concept

1. Node is running with `max_tx_verify_workers ≥ 1` and `max_tx_verify_cycles` set to a large value.
2. Craft a transaction `T` with a script that consumes close to `max_tx_verify_cycles` cycles (e.g., a loop-heavy always-success script).
3. Submit `T` via `send_transaction` RPC. `T` enters `verify_queue` and a worker pops it immediately.
4. While the worker is inside `verify_rtx` (Step 2 of the worker loop), call `remove_transaction(T.hash())`.
5. Observe: `remove_transaction` returns `false` (not found in any store).
6. Wait for verification to complete.
7. Call `get_transaction(T.hash())` — `T` is now in the pool with status `Pending`, despite the removal call.

The root cause is the unguarded gap between `pop_front` releasing the `verify_queue` write lock and `submit_entry` acquiring the `tx_pool` write lock. [7](#0-6) [1](#0-0)

### Citations

**File:** tx-pool/src/process.rs (L96-137)
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

**File:** tx-pool/src/process.rs (L705-754)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        let verified_ret = verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            command_rx,
        )
        .await;

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
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

**File:** tx-pool/src/verify_mgr.rs (L129-162)
```rust
            // pick a entry to run verify
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
                    Some(entry) => entry,
                    None => {
                        if !tasks.is_empty() {
                            tasks.re_notify();
                            debug!(
                                "Worker (role: {:?}) didn't got tx after pop_front, but tasks is not empty, notify other Workers now",
                                self.role
                            );
                        }
                        return;
                    }
                }
            };

            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
```
