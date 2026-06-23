### Title
Verify-Queue Does Not Track In-Flight Input Cells, Allowing Conflicting Transactions to Waste Full CKB-VM Verification Work — (`tx-pool/src/process.rs`, `tx-pool/src/pool_cell.rs`)

---

### Summary

CKB's tx-pool admits transactions into the `VerifyQueue` by checking only for duplicate transaction IDs, not for conflicting input cells. Because `PoolCell` (the cell-availability oracle used during `pre_check`) only consults `pool_map.edges.inputs` — which is populated only after a transaction is fully verified and submitted — two or more distinct transactions spending the same live cell can simultaneously reside in the verify queue and each undergo full, expensive CKB-VM script execution. Only one will ultimately succeed at `submit_entry`; all others waste their verification cycles. An unprivileged tx-pool submitter or relay peer can exploit this to force the node to perform arbitrarily multiplied verification work.

---

### Finding Description

The transaction admission pipeline has three stages:

**Stage 1 — Enqueue** (`resumeble_process_tx`):

```
resumeble_process_tx
  → orphan_contains(tx)       // checks by proposal_short_id only
  → verify_queue_contains(tx) // checks by proposal_short_id only
  → enqueue_verify_queue(tx)
```

The duplicate check is keyed on `ProposalShortId` (a hash of the transaction itself). Two *different* transactions that spend the *same* cell have different IDs and both pass this gate. [1](#0-0) 

**Stage 2 — Pre-check** (`pre_check`, under read lock):

`pre_check` calls `resolve_tx_from_pool`, which constructs a `PoolCell` overlay and calls `resolve_transaction`. `PoolCell::cell()` marks an outpoint as `Dead` only if it appears in `pool_map.edges.inputs`:

```rust
fn cell(&self, out_point: &OutPoint, _eager_load: bool) -> CellStatus {
    if !self.rbf && self.pool_map.edges.get_input_ref(out_point).is_some() {
        return CellStatus::Dead;
    }
    ...
}
``` [2](#0-1) 

Transactions sitting in the `VerifyQueue` are **not** in `pool_map`; their inputs are **not** registered in `pool_map.edges.inputs`. Therefore, when tx_B's `pre_check` runs while tx_A (spending the same cell) is still being verified, the cell appears live and tx_B passes resolution cleanly. [3](#0-2) [4](#0-3) 

**Stage 3 — Submit** (`submit_entry`, under write lock):

Only here does the conflict become visible. The second transaction hits `find_conflict_outpoint` and is rejected with `Reject::Resolve(OutPointError::Dead(...))`. By this point, full CKB-VM script execution has already completed for both transactions. [5](#0-4) 

The `VerifyQueue` itself has no concept of input-cell occupancy: [6](#0-5) 

---

### Impact Explanation

CKB-VM verification is the most expensive operation in the tx-pool pipeline, bounded by `max_tx_verify_cycles` (consensus maximum). An attacker who submits N distinct transactions all spending the same live cell forces the node to run N full CKB-VM verifications, of which N−1 are entirely wasted. The verify queue accepts up to 256 MB of transaction data; within that budget, an attacker can pack many conflicting large-cycle transactions. This constitutes a resource-exhaustion / DoS vector against the verification worker pool, degrading throughput for legitimate transactions and potentially stalling block assembly. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

The attack requires only the ability to submit transactions to the RPC (`send_transaction`) or relay them over P2P. No privileged access, key material, or majority hashpower is needed. Constructing N transactions that all spend the same confirmed cell is trivial. The minimum fee rate (`min_fee_rate`) imposes a small cost on the attacker, but the verification cost imposed on the node scales with `declared_cycles`, which can be up to `max_tx_verify_cycles` per transaction. The cost asymmetry makes this practical. [9](#0-8) 

---

### Recommendation

Track in-flight input cells in the `VerifyQueue` (or a parallel structure). Before enqueuing a new transaction, check whether any of its input `OutPoint`s are already claimed by a transaction currently in the verify queue. If so, either reject the new transaction immediately (ensuring only one in-flight claimant per cell at a time) or apply the same RBF rules used at `submit_entry` time. This mirrors the mitigation in the reference report: "ensure only one swap can be in-flight at a time."

---

### Proof of Concept

1. Identify a live confirmed cell `C` on the network.
2. Construct tx_A and tx_B, both spending `C`, with different outputs (so they have different tx hashes / `ProposalShortId`s). Give both the maximum declared cycle count.
3. Submit tx_A via `send_transaction` RPC → it enters the verify queue.
4. Immediately submit tx_B via `send_transaction` RPC → `verify_queue_contains` returns `false` (different ID), `orphan_contains` returns `false`; tx_B also enters the verify queue.
5. Both transactions are picked up by verify workers and undergo full CKB-VM execution.
6. tx_A completes first and is inserted into `pool_map`.
7. tx_B completes verification but fails at `submit_entry` with `OutPointError::Dead` — all its verification cycles were wasted.
8. Repeat with N transactions to multiply the wasted work by N. [1](#0-0) [10](#0-9)

### Citations

**File:** tx-pool/src/process.rs (L103-116)
```rust
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

**File:** tx-pool/src/process.rs (L335-352)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
```

**File:** tx-pool/src/process.rs (L371-384)
```rust
    pub(crate) async fn submit_remote_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Cycle,
        peer: PeerIndex,
    ) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, false, Some((declared_cycles, peer)))
            .await
    }

    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
    }
```

**File:** tx-pool/src/pool_cell.rs (L19-31)
```rust
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L198-236)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
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
