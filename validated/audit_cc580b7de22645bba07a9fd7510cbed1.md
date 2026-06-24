Audit Report

## Title
TOCTOU Race in `_process_tx` Admits Transactions With Unresolvable Inputs Into the Tx-Pool — (`tx-pool/src/process.rs`)

## Summary
`TxPoolService::_process_tx` resolves inputs under a read lock in `pre_check`, releases that lock, runs script verification without any lock, then finalises admission under a write lock in `submit_entry`. The re-liveness check (`check_rtx_from_pool`) inside `submit_entry` is gated on whether the chain tip hash changed. When a concurrent RBF replacement evicts a parent transaction during the unlocked script-execution window, the child transaction is admitted with a permanently unresolvable input, corrupting `edges.inputs` and enabling a pool-exhaustion attack.

## Finding Description

**Phase 1 – `pre_check` (read lock, then released)**

`pre_check` acquires `self.tx_pool.read().await` inside `with_tx_pool_read_lock`, calls `resolve_tx` which uses `PoolCell::cell` to look up each input in the pool's live-output map. If TX_A is in the pool and produces cell C2, TX_B's input C2 resolves as `CellStatus::Live`. The read lock is released when the closure returns. [1](#0-0) [2](#0-1) 

**Phase 2 – `verify_rtx` (no lock held)**

After `pre_check` returns, `_process_tx` calls `verify_rtx` at line 724 with no lock held. This is async and can be arbitrarily long. During this window, a second worker processes TX_A' (an RBF replacement for TX_A), which acquires the write lock, calls `process_rbf` → `pool_map.remove_entry_and_descendants`, evicting TX_A and its output C2 from the pool. [3](#0-2) [4](#0-3) 

**Phase 3 – `submit_entry` (write lock held)**

The critical guard in `submit_entry`:

```rust
let tip_hash = snapshot.tip_hash();
if pre_resolve_tip != tip_hash {   // FALSE when no new block arrived
    status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;
    ...
}
```

`check_rtx` calls `check_rtx_from_pool`, which would detect that C2 is no longer live. But this entire block is skipped when the tip hash has not changed — which is the common case when only an RBF replacement occurred. [5](#0-4) [6](#0-5) 

Before the tip-hash guard, the code checks for conflicts. When RBF is enabled, `check_rbf` calls `find_conflict_tx`, which only checks `edges.inputs`. Since TX_A was removed, C2 is no longer in `edges.inputs` as a consumed input (C2 was TX_A's *output*, not an input of any transaction), so no conflict is found and `check_rbf` returns `Ok(HashSet::new())`. [7](#0-6) [8](#0-7) 

**Admission with corrupt state**

`_submit_entry` → `add_pending` → `pool_map.add_entry` → `record_entry_edges` calls `insert_input(C2, TX_B_id)`. Because TX_A's removal already called `remove_entry_edges` (which removes TX_A's *inputs*, not its outputs, from `edges.inputs`), C2 was never in `edges.inputs` to begin with. The entry is vacant and `insert_input` succeeds, registering C2 as consumed by TX_B even though C2 does not exist anywhere. [9](#0-8) [10](#0-9) 

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

1. **Invalid transaction permanently in the pool.** TX_B's input C2 is neither on-chain nor in the pool. The block assembler cannot resolve it and silently skips TX_B. TX_B persists until the pool's expiry timer fires or `limit_size` evicts it.
2. **Edge-tracking corruption.** C2 is recorded in `edges.inputs` as consumed by TX_B. Any subsequent transaction that legitimately tries to spend C2 is rejected as a double-spend, even though TX_B is invalid.
3. **Pool exhaustion DoS.** By repeating the pattern, an attacker fills the pool with permanently-stuck invalid entries, evicting legitimate transactions via `limit_size`.

## Likelihood Explanation

- Requires only standard RPC access (`send_transaction`). No privileged role, no majority hashpower.
- Requires RBF to be enabled (non-default but documented configuration).
- The race window equals the full `verify_rtx` duration for TX_B. An attacker maximises this by crafting TX_B with a script near `max_tx_verify_cycles`, while TX_A' uses a trivial always-success script processed almost instantly by a second worker.
- `VerifyMgr` spawns multiple concurrent workers, making the interleaving routine rather than exceptional. [11](#0-10) 
- The attacker pays fees for TX_A and TX_A' but not for TX_B (TX_B is never committed). Cost per injected invalid entry is bounded by two transaction fees.

## Recommendation

In `submit_entry`, unconditionally re-run `check_rtx_from_pool` against the current pool state before calling `_submit_entry`, regardless of whether the tip hash changed. The existing guard should only skip the *time-relative* re-verification, not the pool-state re-verification:

```rust
// Always re-check input liveness against current pool state
status = check_rtx(tx_pool, &snapshot, &entry.rtx)?;

// Only redo time-relative verify if the chain tip advanced
if pre_resolve_tip != tip_hash {
    let tip_header = snapshot.tip_header();
    let tx_env = status.with_env(tip_header);
    time_relative_verify(snapshot, Arc::clone(&entry.rtx), tx_env)?;
}
``` [5](#0-4) 

## Proof of Concept

```
1. Attacker holds live cell C1 on-chain.

2. Submit TX_A (input: C1 → output: C2, trivial always-success script).
   → TX_A enters pool; C2 is live in pool_map outputs.

3. Submit TX_B (input: C2, script near max_tx_verify_cycles).
   → pre_check resolves C2 as Live (TX_A in pool); read lock released.
   → verify_rtx begins; worker is busy for ~seconds.

4. Submit TX_A' (input: C1, higher fee — valid RBF of TX_A).
   → Second worker processes TX_A' quickly (trivial script).
   → submit_entry for TX_A': check_rbf finds TX_A as conflict,
     calls process_rbf → remove_entry_and_descendants(TX_A).
   → TX_A and C2 are gone from pool_map. TX_B is still in verify_queue.

5. TX_B's verify_rtx completes.
   → submit_entry for TX_B:
       check_rbf: find_conflict_tx(TX_B) → C2 not in edges.inputs → empty conflicts.
       pre_resolve_tip == tip_hash (no new block) → check_rtx_from_pool skipped.
       _submit_entry adds TX_B to pool; C2 inserted into edges.inputs.

6. TX_B is now in the pending pool with input C2 that does not exist.
   Block assembler skips TX_B silently.
   Any tx spending C2 is rejected as double-spend.
   Repeat steps 2–5 to exhaust pool capacity.
```

### Citations

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

**File:** tx-pool/src/process.rs (L715-732)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L294-298)
```rust
    pub(crate) fn find_conflict_tx(&self, tx: &TransactionView) -> HashSet<ProposalShortId> {
        tx.input_pts_iter()
            .filter_map(|out_point| self.edges.get_input_ref(&out_point).cloned())
            .collect()
    }
```

**File:** tx-pool/src/component/pool_map.rs (L462-472)
```rust
    fn record_entry_edges(&mut self, entry: &TxEntry) -> Result<(), Reject> {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let header_deps = entry.transaction().header_deps();
        let related_dep_out_points: Vec<_> = entry.related_dep_out_points().cloned().collect();
        let inputs = entry.transaction().input_pts_iter();

        // if input reference a in-pool output, connect it
        // otherwise, record input for conflict check
        for i in inputs {
            self.edges.insert_input(i.to_owned(), tx_short_id.clone())?;
        }
```

**File:** tx-pool/src/pool.rs (L363-370)
```rust
    pub(crate) fn check_rtx_from_pool(&self, rtx: &ResolvedTransaction) -> Result<(), Reject> {
        let snapshot = self.snapshot();
        let pool_cell = PoolCell::new(&self.pool_map, false);
        let checker = OverlayCellChecker::new(&pool_cell, snapshot);
        let mut seen_inputs = HashSet::new();
        rtx.check(&mut seen_inputs, &checker, snapshot)
            .map_err(Reject::Resolve)
    }
```

**File:** tx-pool/src/pool.rs (L574-585)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
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
