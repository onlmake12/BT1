### Title
TOCTOU Race in `_process_tx` Allows Redundant Script Verification via Concurrent Duplicate Submissions — (`tx-pool/src/process.rs`)

---

### Summary

`_process_tx` in `tx-pool/src/process.rs` performs the duplicate-transaction check (`check_txid_collision`) under a **read lock** in `pre_check`, then releases all locks to run expensive CKB-VM script verification (`verify_rtx`), and finally re-acquires a **write lock** in `submit_entry` **without re-checking the duplicate condition**. This is a direct Checks-Effects-Interactions pattern violation: the check and the effect are not atomic. A malicious peer can exploit the resulting TOCTOU window to force the node to run full script verification multiple times for the same transaction, exhausting CPU resources.

---

### Finding Description

The transaction admission pipeline in `_process_tx` follows this sequence:

**Step 1 — Check (read lock acquired and released):**
`pre_check` acquires `tx_pool.read()`, calls `check_txid_collision` to reject duplicates by `ProposalShortId`, resolves inputs, and checks the fee. The read lock is **dropped** before returning. [1](#0-0) 

**Step 2 — Interaction (no lock held):**
`verify_rtx` runs the full `ContextualTransactionVerifier` (CKB-VM script execution) with **no lock held**. This is the most expensive step and can take an arbitrarily long time for complex scripts. [2](#0-1) [3](#0-2) 

**Step 3 — Effect (write lock acquired):**
`submit_entry` acquires `tx_pool.write()`. It re-checks for **conflicting inputs** (via `find_conflict_outpoint` / `check_rbf`) and, if the chain tip changed, re-runs time-relative verification. However, it **never re-calls `check_txid_collision`**. The comment in the code explicitly acknowledges that `check_rbf` must be inside the write lock to avoid concurrent issues — but the analogous protection for duplicate detection is absent. [4](#0-3) [5](#0-4) 

The full call chain in `_process_tx`: [6](#0-5) 

The duplicate check itself: [7](#0-6) 

The `resumeble_process_tx` path (used for remote peer submissions) has a parallel TOCTOU: `verify_queue_contains` is checked under a read lock on `verify_queue`, then the lock is released, and `enqueue_verify_queue` acquires a separate write lock — with no atomic check-and-enqueue: [8](#0-7) [9](#0-8) 

---

### Impact Explanation

**CPU exhaustion / DoS via redundant script verification.**

A malicious peer submits the same valid transaction T (with maximally complex scripts, up to `max_block_cycles`) N times concurrently. Because the TOCTOU window spans the entire duration of `verify_rtx`, all N submissions can pass `check_txid_collision` before any of them is inserted into the pool. The verify manager then runs N full CKB-VM executions for the same transaction. Only one succeeds; the remaining N−1 are rejected — but only after consuming N−1 full verification cycles.

Secondary impact: the N−1 rejected submissions are caught by `find_conflict_outpoint` (same inputs as the already-inserted copy), which returns `Reject::Resolve(OutPointError::Dead)`. This causes `after_process` to record them in `conflicts_pool` (the conflicts LRU cache) instead of `recent_reject`, polluting the conflict cache with valid transactions and potentially evicting legitimate conflict records. [10](#0-9) 

---

### Likelihood Explanation

**Medium.** Any unprivileged peer can submit transactions via the P2P relay protocol. Sending the same transaction multiple times in rapid succession is trivially achievable with a standard CKB client or a crafted peer. The TOCTOU window is wide — it spans the entire CKB-VM execution time, which for a max-cycles transaction can be hundreds of milliseconds. No special privileges, keys, or majority hashpower are required.

---

### Recommendation

Re-check `check_txid_collision` (or equivalently, `contains_proposal_id`) **inside `submit_entry` under the write lock**, before calling `_submit_entry`. This mirrors the existing pattern where `check_rbf` is explicitly placed inside the write lock to prevent concurrent issues:

```rust
// Inside submit_entry's write-lock closure, before _submit_entry:
if tx_pool.contains_proposal_id(&entry.proposal_short_id()) {
    return Err(Reject::Duplicated(entry.transaction().hash()));
}
```

Additionally, make `enqueue_verify_queue` perform an atomic check-and-insert (check for existing entry inside the write lock on `verify_queue`) to close the parallel TOCTOU in `resumeble_process_tx`. [11](#0-10) [12](#0-11) 

---

### Proof of Concept

1. Attacker peer constructs a valid transaction T whose lock script consumes close to `max_block_cycles` cycles.
2. Attacker opens a P2P connection and sends T via `RelayTransactions` N times in rapid succession (e.g., N = 10, in parallel goroutines/threads).
3. All N submissions enter `resumeble_process_tx` → `enqueue_verify_queue` concurrently. Because `verify_queue_contains` is checked under a read lock that is released before `enqueue_verify_queue` acquires its write lock, all N pass the duplicate check and are enqueued.
4. The verify manager dequeues all N and calls `_process_tx` for each. All N pass `pre_check` (T is not yet in `pool_map`). All N call `verify_rtx` concurrently, each running a full CKB-VM execution.
5. The first to complete `submit_entry` inserts T into the pool. The remaining N−1 reach `submit_entry`, find a conflicting outpoint (T's inputs are now marked as spent by the pool), and return `Reject::Resolve(OutPointError::Dead)`.
6. `after_process` records each of the N−1 rejected submissions in `conflicts_pool` (wrong reject category).
7. Net result: the node performed N CKB-VM executions instead of 1, and the `conflicts_pool` is polluted with N−1 spurious entries. [13](#0-12) [14](#0-13)

### Citations

**File:** tx-pool/src/process.rs (L96-116)
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
```

**File:** tx-pool/src/process.rs (L118-137)
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

                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;
```

**File:** tx-pool/src/process.rs (L237-245)
```rust
    pub(crate) async fn verify_queue_contains(&self, tx: &TransactionView) -> bool {
        let queue = self.verify_queue.read().await;
        queue.contains_key(&tx.proposal_short_id())
    }

    pub(crate) async fn orphan_contains(&self, tx: &TransactionView) -> bool {
        let orphan = self.orphan.read().await;
        orphan.contains_key(&tx.proposal_short_id())
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

**File:** tx-pool/src/process.rs (L335-353)
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
    }
```

**File:** tx-pool/src/process.rs (L401-426)
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
    }
```

**File:** tx-pool/src/process.rs (L458-487)
```rust
    pub(crate) async fn after_process(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
        _snapshot: &Snapshot,
        ret: &Result<Completed, Reject>,
    ) {
        let tx_hash = tx.hash();

        // log tx verification result for monitor node
        if log_enabled_target!("ckb_tx_monitor", Trace)
            && let Ok(c) = ret
        {
            trace_target!(
                "ckb_tx_monitor",
                r#"{{"tx_hash":"{:#x}","cycles":{}}}"#,
                tx_hash,
                c.cycles
            );
        }

        if matches!(
            ret,
            Err(Reject::RBFRejected(..) | Reject::Resolve(OutPointError::Dead(_)))
        ) {
            let mut tx_pool = self.tx_pool.write().await;
            if tx_pool.pool_map.find_conflict_outpoint(&tx).is_some() {
                tx_pool.record_conflict(tx.clone());
            }
        }
```

**File:** tx-pool/src/process.rs (L705-777)
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

        self.notify_block_assembler(status).await;

        if verify_cache.is_none() {
            // update cache
            let txs_verify_cache = Arc::clone(&self.txs_verify_cache);
            tokio::spawn(async move {
                let mut guard = txs_verify_cache.write().await;
                guard.put(wtx_hash, verified);
            });
        }

        if let Some(metrics) = ckb_metrics::handle() {
            let elapsed = instant.elapsed().as_secs_f64();
            if is_sync_process {
                metrics.ckb_tx_pool_sync_process.observe(elapsed);
            } else {
                metrics.ckb_tx_pool_async_process.observe(elapsed);
            }
        }

        Some((Ok(verified), submit_snapshot))
    }
```

**File:** tx-pool/src/process.rs (L860-868)
```rust
    async fn enqueue_verify_queue(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let mut queue = self.verify_queue.write().await;
        queue.add_tx(tx, is_proposal_tx, remote)
    }
```

**File:** tx-pool/src/util.rs (L20-26)
```rust
pub(crate) fn check_txid_collision(tx_pool: &TxPool, tx: &TransactionView) -> Result<(), Reject> {
    let short_id = tx.proposal_short_id();
    if tx_pool.contains_proposal_id(&short_id) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    Ok(())
}
```

**File:** tx-pool/src/util.rs (L85-131)
```rust
pub(crate) async fn verify_rtx(
    snapshot: Arc<Snapshot>,
    rtx: Arc<ResolvedTransaction>,
    tx_env: Arc<TxVerifyEnv>,
    cache_entry: &Option<CacheEntry>,
    max_tx_verify_cycles: Cycle,
    command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
) -> Result<Completed, Reject> {
    let consensus = snapshot.cloned_consensus();
    let data_loader = snapshot.as_data_loader();

    if let Some(completed) = cache_entry {
        TimeRelativeTransactionVerifier::new(rtx, consensus, data_loader, tx_env)
            .verify()
            .map(|_| *completed)
            .map_err(Reject::Verification)
    } else if let Some(command_rx) = command_rx {
        ContextualTransactionVerifier::new(
            Arc::clone(&rtx),
            consensus,
            data_loader,
            Arc::clone(&tx_env),
        )
        .verify_with_pause(max_tx_verify_cycles, command_rx)
        .await
        .and_then(|result| {
            DaoScriptSizeVerifier::new(rtx, snapshot.cloned_consensus(), snapshot.as_data_loader())
                .verify()?;
            Ok(result)
        })
        .map_err(Reject::Verification)
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** tx-pool/src/pool.rs (L151-154)
```rust
    /// Returns true if the tx-pool contains a tx with specified id.
    pub(crate) fn contains_proposal_id(&self, id: &ProposalShortId) -> bool {
        self.pool_map.get_by_id(id).is_some()
    }
```
