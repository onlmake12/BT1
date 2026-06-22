### Title
`remove_tx` Non-Atomic Multi-Pool Check Allows Transaction to Persist in Pool Despite Removal — (`tx-pool/src/process.rs`)

---

### Summary

`TxPoolService::remove_tx` checks three separate data structures (verify queue, orphan pool, tx pool) by acquiring and releasing their locks sequentially. A transaction that has been popped from the verify queue by a background worker but not yet committed to the tx pool is invisible to all three checks. The function returns `false` ("not found"), while the worker subsequently submits the transaction to the tx pool. The race window equals the duration of `verify_rtx` (script execution), which a submitter can inflate to near `max_block_cycles`.

---

### Finding Description

`remove_tx` in `tx-pool/src/process.rs` acquires three independent async write locks one after another:

```rust
// tx-pool/src/process.rs:440-456
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    {
        let mut queue = self.verify_queue.write().await;   // lock-1
        if queue.remove_tx(&id).is_some() { return true; }
    }                                                       // lock-1 released
    {
        let mut orphan = self.orphan.write().await;        // lock-2
        if orphan.remove_orphan_tx(&id).is_some() { return true; }
    }                                                       // lock-2 released
    let mut tx_pool = self.tx_pool.write().await;          // lock-3
    tx_pool.remove_tx(&id)
}
```

Concurrently, a `VerifyMgr` worker pops a transaction from the verify queue under its own write lock and then runs the expensive `verify_rtx` step **without holding any lock**:

```rust
// tx-pool/src/verify_mgr.rs:130-145
let entry = {
    let mut tasks = self.tasks.write().await;   // pops from verify_queue
    match tasks.pop_front(...) { ... }
};                                              // lock released here

// _process_tx runs: pre_check → verify_rtx (no lock) → submit_entry
if let Some((res, snapshot)) = self.service._process_tx(...).await { ... }
```

The `_process_tx` flow is:

```rust
// tx-pool/src/process.rs:715-753
let (ret, snapshot) = self.pre_check(&tx).await;          // read lock, released
// ... verify_rtx runs here with NO lock held ...
let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await; // write lock
```

**Race sequence:**

| Step | Worker | `remove_tx` caller |
|------|--------|--------------------|
| 1 | Pops tx from verify_queue (lock released) | — |
| 2 | Runs `verify_rtx` (no lock held) | Acquires verify_queue lock → tx not found → releases |
| 3 | Still in `verify_rtx` | Acquires orphan lock → tx not found → releases |
| 4 | Still in `verify_rtx` | Acquires tx_pool lock → tx not found → releases → **returns `false`** |
| 5 | Calls `submit_entry`, tx enters tx_pool | — |

The transaction is now in the pool despite `remove_transaction` returning `false`.

The race window is the full duration of `verify_rtx`. A submitter can widen this window by crafting a transaction whose scripts consume close to `max_block_cycles` (consensus-enforced maximum), making the window reliably exploitable.

---

### Impact Explanation

An operator or node administrator calling the `remove_transaction` RPC receives `false` ("transaction not found") and believes the transaction has been purged. The transaction is silently re-admitted to the pending pool by the worker. The operator has no indication that the removal failed; the transaction will be proposed and eventually committed to a block. This undermines the only mechanism available to remove a specific transaction from the pool without clearing the entire pool.

---

### Likelihood Explanation

The race window is bounded below by the time to verify an expensive script. A submitter who wants their transaction to survive a removal attempt can submit a transaction with scripts that consume a large fraction of `max_block_cycles`. The CKB tx-pool runs multiple concurrent verify workers (`max_tx_verify_workers`), so the window is open for every transaction currently under verification. No special network position or privileged access is required — any RPC-accessible node is affected. The submitter only needs to time the `remove_transaction` call to land while their transaction is under verification, which is straightforward given the long verification window.

---

### Recommendation

Replace the three sequential lock acquisitions with a single atomic check. One approach: introduce a single `RwLock` that guards all three sub-pools, or add a "pending removal" marker set that is checked by `submit_entry` under the tx_pool write lock before inserting. Concretely, `submit_entry` should consult a shared "to-be-removed" set (populated by `remove_tx` before it releases the verify_queue lock) and abort insertion if the transaction's id is present.

---

### Proof of Concept

1. Submit a transaction `T` whose lock script loops for ~`max_block_cycles` cycles via `send_transaction` RPC.
2. Observe that `T` enters the verify queue (check via `get_raw_tx_pool`).
3. Wait for a verify worker to pop `T` from the queue (verify queue count drops by 1, pending count still 0).
4. Immediately call `remove_transaction(T.hash)` — the call returns `false` ("not found").
5. Wait for verification to complete.
6. Call `get_raw_tx_pool` — `T` appears in the pending pool despite step 4 returning `false`.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** tx-pool/src/process.rs (L96-134)
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

**File:** tx-pool/src/process.rs (L705-753)
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
