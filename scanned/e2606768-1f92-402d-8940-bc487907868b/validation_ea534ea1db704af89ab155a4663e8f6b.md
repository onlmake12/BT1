### Title
TOCTOU Race in tx-pool Duplicate Detection Enables Double Script Verification — (`tx-pool/src/process.rs`)

---

### Summary
The transaction pool's admission pipeline in `tx-pool/src/process.rs` performs duplicate-existence checks under short-lived read locks that are released before the expensive script-verification step begins. Because the locks are not held across the full check-then-act sequence, two concurrent callers submitting the same transaction can both pass the duplicate guard and both proceed to full CKB-VM script verification. The second submission is ultimately rejected under the write lock, but the CPU cost of a complete script execution is paid twice. A network peer can deliberately trigger this to exhaust node CPU resources.

---

### Finding Description

The transaction admission pipeline in `_process_tx` follows a three-phase pattern:

**Phase 1 — `pre_check` (read lock, then released)** [1](#0-0) 

Inside `pre_check`, a read lock is acquired on `tx_pool`, the transaction is resolved against the current snapshot, and the fee is checked. The lock is released when `pre_check` returns.

**Phase 2 — `verify_rtx` (no lock held)** [2](#0-1) 

Full CKB-VM script execution runs here with no lock held. This is the most expensive step and can take significant CPU time for complex scripts.

**Phase 3 — `submit_entry` (write lock)** [3](#0-2) 

The write lock is acquired and the entry is inserted into the pool.

The duplicate-existence checks in `resumeble_process_tx` are each guarded by separate, short-lived read locks:

```
orphan_contains  → READ lock on orphan  → released
verify_queue_contains → READ lock on verify_queue → released
enqueue_verify_queue  → WRITE lock on verify_queue
``` [4](#0-3) 

Similarly in `process_tx`: [5](#0-4) 

Between the last read-lock release and the write-lock acquisition in `submit_entry`, there is an unguarded window. Two concurrent async tasks (e.g., two relay peers sending the same transaction) can both observe the pool as not containing the transaction, both pass the duplicate guard, and both enter Phase 2 simultaneously.

The `submit_entry` write-lock re-check handles the case where the chain tip changed: [6](#0-5) 

But it does **not** prevent a second concurrent caller from completing Phase 2 (full script verification) before the first caller's write lock is released. The second caller's `_submit_entry` will fail gracefully via the edges conflict check: [7](#0-6) 

State is not corrupted, but the full cost of script verification is paid twice.

---

### Impact Explanation

An attacker controlling two or more peers can relay the same transaction simultaneously to a target node. Each relay triggers an independent call to `resumeble_process_tx` or `process_tx`. Both calls pass the duplicate guard (TOCTOU window) and both execute full CKB-VM script verification. For a transaction with a computationally expensive lock or type script (up to `max_block_cycles`), this doubles the CPU cost per submission. By continuously relaying the same or different transactions from multiple peers, an attacker can sustain elevated CPU load on the node, degrading block validation throughput and peer responsiveness.

---

### Likelihood Explanation

Any unprivileged network peer can relay transactions. The TOCTOU window exists whenever two relay messages for the same transaction arrive close together in time, which is a normal network condition (e.g., transaction broadcast propagation). No special privileges, keys, or majority hashpower are required. The attacker only needs to connect to the target node as two peers and send the same transaction from both simultaneously.

---

### Recommendation

Consolidate the duplicate check and the enqueue/verify-start into a single critical section protected by a write lock, so that the check-then-act sequence is atomic. Alternatively, use a dedicated in-flight set (protected by a single lock) that is updated atomically when a transaction enters verification, preventing a second caller from proceeding past the duplicate check until the first caller's verification completes or fails.

---

### Proof of Concept

1. Attacker connects to the target node as two distinct peers, `P1` and `P2`.
2. Both peers simultaneously send a `RelayTransactions` message containing the same transaction `T` with a maximally expensive script (cycles near `max_block_cycles`).
3. The node's relay handler calls `resumeble_process_tx` for each peer concurrently.
4. Both calls execute `orphan_contains` (returns false, lock released) and `verify_queue_contains` (returns false, lock released) before either enqueues.
5. Both calls proceed to `enqueue_verify_queue` / `_process_tx` → `pre_check` → `verify_rtx`.
6. Both calls execute full CKB-VM script verification for `T`.
7. The first call to reach `submit_entry` succeeds; the second fails with `RBFRejected` from `insert_input`.
8. Net result: the node paid twice the CPU cost for a single transaction admission. Repeating this continuously sustains a 2× CPU amplification on the node's verification pipeline. [4](#0-3) [8](#0-7) [9](#0-8) [7](#0-6)

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

**File:** tx-pool/src/process.rs (L401-425)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
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
