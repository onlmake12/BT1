### Title
TOCTOU: Stale Cell-Liveness Check Allows Invalid Transactions Into the Tx-Pool — (`tx-pool/src/process.rs`)

### Summary

`TxPoolService::_process_tx` resolves and validates a transaction's inputs under a **read lock**, releases that lock, runs script verification without any lock, then finalises admission under a **write lock**. When the chain tip has not advanced between those two lock acquisitions, `submit_entry` skips the re-check of input liveness (`check_rtx_from_pool`). A concurrent RBF replacement that removes a parent transaction from the pool during the unlocked script-execution window causes a child transaction to be admitted with a permanently unresolvable input, silently corrupting the pool's edge-tracking state.

---

### Finding Description

`_process_tx` follows a three-phase pipeline:

**Phase 1 – `pre_check` (read lock held, then released)**

```
let (ret, snapshot) = self.pre_check(&tx).await;
// read lock is dropped here; tip_hash captured
let (tip_hash, rtx, status, fee, tx_size) = ...;
```

`pre_check` calls `resolve_tx_from_pool`, which uses `OverlayCellProvider` to look up each input in the pool's live-output map. If parent TX_A is in the pool and produces cell C2, TX_B's input C2 resolves as `CellStatus::Live`. The read lock is then released.

**Phase 2 – `verify_rtx` (no lock held)**

```
let verified_ret = verify_rtx(
    Arc::clone(&snapshot), Arc::clone(&rtx), tx_env,
    &verify_cache, max_cycles, command_rx,
).await;
```

Script execution is async and can be arbitrarily long. During this window, another worker or the main service can process TX_A' (an RBF replacement for TX_A), which calls `process_rbf` → `pool_map.remove_entry_and_descendants(&id)`, evicting TX_A and its output C2 from the pool.

**Phase 3 – `submit_entry` (write lock held)**

```
let conflicts = if tx_pool.enable_rbf() {
    tx_pool.check_rbf(&snapshot, &entry)?
} else {
    let conflicted_outpoint =
        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
    if let Some(outpoint) = conflicted_outpoint {
        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
    }
    HashSet::new()
};

let tip_hash = snapshot.tip_hash();
if pre_resolve_tip != tip_hash {          // ← guard is FALSE when tip unchanged
    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;
    ...
    time_relative_verify(...)?;
}
// proceeds to _submit_entry without re-checking input liveness
```

`find_conflict_outpoint` only checks `edges.inputs` for direct double-spends; it does **not** verify that each input is actually live in the pool or on-chain. Because the tip hash has not changed (no new block arrived), the `pre_resolve_tip != tip_hash` guard is `false`, so `check_rtx_from_pool` is never called. TX_B is inserted into `pool_map` with C2 registered in `edges.inputs`, even though C2 no longer exists anywhere.

---

### Impact Explanation

1. **Invalid transaction permanently in the pool.** TX_B's input C2 is neither on-chain nor in the pool. The block assembler cannot resolve it and silently skips TX_B. TX_B persists until the pool's expiry timer fires or `limit_size` evicts it.

2. **Edge-tracking corruption.** C2 is recorded in `edges.inputs` as consumed by TX_B. Any subsequent transaction that legitimately tries to spend C2 (e.g., after a reorg that re-introduces TX_A) is rejected as a double-spend, even though TX_B is invalid.

3. **Tx-pool DoS.** An attacker who controls script complexity can widen the Phase 2 window arbitrarily. By repeating the pattern (TX_A → TX_B → TX_A' RBF) the attacker fills the pool with permanently-stuck invalid entries, evicting legitimate transactions via `limit_size`.

---

### Likelihood Explanation

- The attack requires only standard RPC access (`send_transaction`). No privileged role, no majority hashpower, no social engineering.
- The race window is the entire duration of `verify_rtx` for TX_B. An attacker can maximise this by crafting TX_B with a script that consumes close to `max_tx_verify_cycles`, while TX_A' uses a trivial always-success script that is processed almost instantly by a second worker.
- The `VerifyMgr` spawns multiple concurrent workers (`max_tx_verify_workers`), making the interleaving routine rather than exceptional.
- The attacker pays fees for TX_A and TX_A' but not for TX_B (TX_B is never committed). The cost per injected invalid entry is bounded by two transaction fees.

---

### Recommendation

In `submit_entry`, unconditionally re-run `check_rtx_from_pool` (input liveness against the current pool state) before calling `_submit_entry`, regardless of whether the tip hash changed. The existing guard should only skip the *time-relative* re-verification, not the pool-state re-verification:

```rust
// Always re-check input liveness against current pool state
status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

// Only redo time-relative verify if the chain tip advanced
if pre_resolve_tip != tip_hash {
    let tip_header = snapshot.tip_header();
    let tx_env = status.with_env(tip_header);
    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
}
```

---

### Proof of Concept

```
1. Attacker holds live cell C1 on-chain.

2. Submit TX_A  (input: C1 → output: C2, always-success lock, trivial script)
   → TX_A enters pool; C2 is live in pool_map outputs.

3. Submit TX_B  (input: C2, complex script near max_tx_verify_cycles)
   → pre_check resolves C2 as Live (TX_A in pool); read lock released.
   → verify_rtx begins; worker is busy for ~seconds.

4. Submit TX_A' (input: C1, higher fee — valid RBF of TX_A)
   → second worker processes TX_A' quickly (trivial script).
   → submit_entry for TX_A': check_rbf finds TX_A as conflict,
     calls process_rbf → remove_entry_and_descendants(TX_A).
   → TX_A and C2 are gone from pool_map. TX_B is still in verify_queue.

5. TX_B's verify_rtx completes.
   → submit_entry for TX_B:
       find_conflict_outpoint(TX_B): C2 not in edges.inputs → no conflict found.
       pre_resolve_tip == tip_hash (no new block) → check_rtx_from_pool skipped.
       _submit_entry adds TX_B to pool; C2 inserted into edges.inputs.

6. TX_B is now in the pending pool with input C2 that does not exist.
   Block assembler skips TX_B silently.
   Any tx spending C2 is rejected as double-spend.
   Repeat steps 2-5 to exhaust pool capacity.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/process.rs (L269-316)
```rust
    pub(crate) async fn pre_check(
        &self,
        tx: &TransactionView,
    ) -> (Result<PreCheckedTx, Reject>, Arc<Snapshot>) {
        // Acquire read lock for cheap check
        let tx_size = tx.data().serialized_size_in_block();

        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
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

**File:** tx-pool/src/pool_cell.rs (L18-31)
```rust
impl<'a> CellProvider for PoolCell<'a> {
    fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
        if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
            return CellStatus::Dead;
        }
        if let Some((output, data)) = self.pool_map.get_output_with_data(out_point) {
            let cell_meta = CellMetaBuilder::from_cell_output(output, data)
                .out_point(out_point.to_owned())
                .build();
            CellStatus::live_cell(cell_meta)
        } else {
            CellStatus::Unknown
        }
    }
```

**File:** tx-pool/src/component/edges.rs (L33-54)
```rust
    pub(crate) fn insert_input(
        &mut self,
        out_point: OutPoint,
        txid: ProposalShortId,
    ) -> Result<(), Reject> {
        // inputs is occupied means double speanding happened here
        match self.inputs.entry(out_point.clone()) {
            Entry::Occupied(occupied) => {
                let msg = format!(
                    "txpool unexpected double-spending out_point: {:?} old_tx: {:?} new_tx: {:?}",
                    out_point,
                    occupied.get(),
                    txid
                );
                Err(Reject::RBFRejected(msg))
            }
            Entry::Vacant(vacant) => {
                vacant.insert(txid);
                Ok(())
            }
        }
    }
```

**File:** tx-pool/src/verify_mgr.rs (L109-163)
```rust
    async fn process_inner(&mut self) {
        loop {
            if self.exit_signal.is_cancelled() {
                info!("Verify worker::process_inner exit_signal is cancelled");
                return;
            }
            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }
            // cheap query to check queue is not empty
            if self.tasks.read().await.is_empty() {
                return;
            }

            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }

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
    }
```
