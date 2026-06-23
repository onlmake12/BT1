### Title
Stale Tx-Pool Snapshot Allows Dead-Cell Transactions to Bypass Admission Validation — (`tx-pool/src/pool.rs`, `tx-pool/src/process.rs`)

---

### Summary

The CKB transaction pool (`TxPool`) holds its own `Arc<Snapshot>` that is updated **asynchronously** after each block is committed to the chain. During the window between block commitment and snapshot update, a transaction spending an already-committed (dead) cell passes cell-liveness validation and is admitted to the mempool. The partial re-check in `submit_entry` only fires when the snapshot changed between `pre_check` and `submit_entry`; if the snapshot remains stale throughout the entire admission pipeline, no re-validation occurs and the invalid transaction enters the pool.

---

### Finding Description

The `TxPool` struct stores a `snapshot` field that is explicitly documented as potentially inconsistent with the chain: [1](#0-0) 

The snapshot is updated only when `_update_tx_pool_for_reorg` runs, which is triggered asynchronously via a bounded channel `try_send` call in `chain/src/verify.rs`: [2](#0-1) 

The `try_send` can silently fail (only logs an error) if the channel is full, leaving the tx-pool snapshot stale for an indefinite number of blocks.

The full transaction admission path in `_process_tx` is:

1. **`pre_check`** acquires a read lock and clones the tx-pool's (potentially stale) snapshot: [3](#0-2) 

2. **`resolve_tx_from_pool`** uses `OverlayCellProvider::new(&pool_cell, snapshot)` where `snapshot` is the stale one. A cell spent in a recently committed block (not yet reflected in the stale snapshot) appears `Live`: [4](#0-3) 

3. **`verify_rtx`** runs `ContextualTransactionVerifier` (or `TimeRelativeTransactionVerifier` for cached entries) using the stale snapshot's data loader and the stale `tip_header` as `tx_env`: [5](#0-4) 

4. **`submit_entry`** contains a partial mitigation — it re-checks cell liveness only if the snapshot tip changed between `pre_check` and `submit_entry`: [6](#0-5) 

If the snapshot has **not** been updated by the time `submit_entry` runs (i.e., `pre_resolve_tip == tip_hash` because both are from the same stale snapshot), the guard at line 120 is **not entered**, and the transaction is unconditionally inserted into the pool via `_submit_entry` with a dead input cell.

---

### Impact Explanation

A transaction spending a cell that was already consumed in a committed block is admitted to the mempool. Concretely:

- The invalid transaction occupies pool space and is relayed to connected peers via the P2P relay protocol, potentially propagating the invalid entry across the network.
- Peers running the same code have the same stale-snapshot window and may also admit the transaction.
- The transaction can never be mined into a valid block (block-level verification is independent of the tx-pool), but it persists in the pool until `update_tx_pool_for_reorg` eventually runs and `resolve_conflict` evicts it.
- If `try_send` on the reorg channel fails (channel full under load), the snapshot remains stale across multiple blocks, widening the window significantly and allowing an attacker to flood the mempool with many such invalid entries. [7](#0-6) 

---

### Likelihood Explanation

The vulnerability is reachable by any unprivileged RPC caller or P2P transaction sender. The attacker's strategy:

1. Spend a cell in a transaction that gets committed in a block.
2. Immediately submit a second transaction spending the same cell to the node's RPC (`send_transaction`) or via P2P relay, timed to arrive before the tx-pool snapshot is updated.
3. The window is normally milliseconds, but is extended whenever the reorg channel is under backpressure (e.g., rapid block arrival, high tx throughput), making the attack more reliable under load.

No special privileges, keys, or majority hashpower are required.

---

### Recommendation

1. **Re-validate cell liveness unconditionally in `submit_entry`**, not only when the snapshot tip changed. The `check_rtx` call (which uses `OverlayCellChecker` against the current snapshot) should run regardless of whether `pre_resolve_tip == tip_hash`.
2. **Replace `try_send` with a bounded but non-lossy send** for the reorg notification, or ensure the tx-pool snapshot is updated synchronously before the chain service returns, to eliminate the stale-snapshot window entirely.
3. Alternatively, after `update_tx_pool_for_reorg`, sweep remaining pool entries against the new snapshot to evict any entries whose inputs are now dead.

---

### Proof of Concept

**Setup:**
- Node N with tx-pool snapshot at block height H (stale; actual chain tip is H+1 where cell C was spent by Tx A).

**Steps:**
1. Attacker observes Tx A (spending cell C) being committed in block H+1.
2. Before the tx-pool snapshot updates to H+1, attacker submits Tx B (also spending cell C) via `send_transaction` RPC.
3. `pre_check` runs: `resolve_tx_from_pool` uses the stale snapshot at H → cell C appears `Live` → Tx B resolves successfully.
4. `verify_rtx` runs with stale snapshot → passes.
5. `submit_entry` runs: `pre_resolve_tip == snapshot.tip_hash()` (both are H) → the guard at line 120 is **not entered** → Tx B is inserted into the pool.
6. Tx B is relayed to peers. Peers with the same stale window also admit it.
7. Eventually `update_tx_pool_for_reorg` runs and `resolve_conflict` evicts Tx B — but the invalid entry was live in the pool and propagated across the network during the window. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/pool.rs (L70-73)
```rust
    /// Tx-pool owned snapshot, it may not consistent with chain cause tx-pool update snapshot asynchronously
    pub(crate) fn snapshot(&self) -> &Snapshot {
        &self.snapshot
    }
```

**File:** tx-pool/src/pool.rs (L253-268)
```rust
    fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
        let short_id = tx.proposal_short_id();
        if let Some(_entry) = self.pool_map.remove_entry(&short_id) {
            debug!("remove_committed_tx for {}", tx.hash());
        }
        {
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L372-384)
```rust
    pub(crate) fn resolve_tx_from_pool(
        &self,
        tx: TransactionView,
        rbf: bool,
    ) -> Result<Arc<ResolvedTransaction>, Reject> {
        let snapshot = self.snapshot();
        let pool_cell = PoolCell::new(&self.pool_map, rbf);
        let provider = OverlayCellProvider::new(&pool_cell, snapshot);
        let mut seen_inputs = HashSet::new();
        resolve_transaction(tx, &mut seen_inputs, &provider, snapshot)
            .map(Arc::new)
            .map_err(Reject::Resolve)
    }
```

**File:** chain/src/verify.rs (L385-398)
```rust
            let tx_pool_controller = self.shared.tx_pool_controller();
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
                if let Err(e) = tx_pool_controller.update_ibd_state(in_ibd) {
                    error!("Notify update_ibd_state error {}", e);
                }
            }
```

**File:** tx-pool/src/process.rs (L118-134)
```rust
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

**File:** tx-pool/src/process.rs (L247-256)
```rust
    pub(crate) async fn with_tx_pool_read_lock<U, F: FnMut(&TxPool, Arc<Snapshot>) -> U>(
        &self,
        mut f: F,
    ) -> (U, Arc<Snapshot>) {
        let tx_pool = self.tx_pool.read().await;
        let snapshot = tx_pool.cloned_snapshot();

        let ret = f(&tx_pool, Arc::clone(&snapshot));
        (ret, snapshot)
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

**File:** tx-pool/src/service.rs (L241-258)
```rust
    pub fn update_tx_pool_for_reorg(
        &self,
        detached_blocks: VecDeque<BlockView>,
        attached_blocks: VecDeque<BlockView>,
        detached_proposal_id: HashSet<ProposalShortId>,
        snapshot: Arc<Snapshot>,
    ) -> Result<(), AnyError> {
        let notify = Notify::new((
            detached_blocks,
            attached_blocks,
            detached_proposal_id,
            snapshot,
        ));
        self.reorg_sender.try_send(notify).map_err(|e| {
            let (_m, e) = handle_try_send_error(e);
            e.into()
        })
    }
```
