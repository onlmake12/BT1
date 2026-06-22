### Title
TOCTOU Race in `_process_tx` Allows Spurious Tx-Pool Eviction via Concurrent Submission Paths — (`tx-pool/src/process.rs`)

### Summary

`_process_tx` in `tx-pool/src/process.rs` performs a txid-collision check under a **read lock** in `pre_check`, releases that lock, then runs the expensive `verify_rtx` script-execution step with **no lock held**, and finally calls `submit_entry` under a **write lock** that does **not** re-check for txid collision. Because the verify queue workers and the direct RPC `process_tx` path run as independent concurrent tokio tasks, two concurrent invocations of `_process_tx` for the same transaction can both pass `pre_check`, both complete `verify_rtx`, and then both enter `submit_entry`. The second `submit_entry` silently skips insertion (the pool's `add_entry` returns `(false, evicts)` for a duplicate) but still calls `limit_size`, which can evict a legitimate third-party transaction from the pool. Both calls also return `Ok(verified)`, causing `after_process` to fire twice, double-relaying the transaction to peers and double-processing orphan dependents.

### Finding Description

**Root cause — three-phase unlock gap in `_process_tx`:**

```
pre_check()          ← read lock acquired + released
    check_txid_collision()   ← only check for duplicate
    resolve_tx()
    check_tx_fee()
                     ← READ LOCK RELEASED HERE
verify_rtx()         ← no lock, long async script execution
submit_entry()       ← write lock acquired
    check_rbf()      ← re-checked under write lock
    tip_hash check   ← re-checked under write lock
    _submit_entry()  ← txid collision NOT re-checked
    limit_size()     ← called even when succ == false
```

**Concurrent entry paths that trigger the race:**

- **Path A (RPC):** `send_transaction` RPC → `TxPoolController::submit_local_tx` → service message loop → `process_tx` → `_process_tx` (direct, no queue).
- **Path B (P2P relay):** P2P `RelayTransaction` → `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` → verify queue worker task → `_process_tx`.

Both paths are independent tokio tasks. During the `verify_rtx` await point in Path A, the tokio runtime freely schedules Path B's worker. Both pass `pre_check` because neither has inserted the tx yet (read lock sees an empty slot). Both complete `verify_rtx`. Both then call `submit_entry` under the write lock (serialized):

- **First `submit_entry`:** `pool_map.add_entry` returns `(true, evicts)` → tx inserted → `limit_size` called → if pool is at capacity, one legitimate tx is evicted.
- **Second `submit_entry`:** `pool_map.add_entry` returns `(false, evicts)` (duplicate silently ignored, line 207–208 of `pool_map.rs`) → tx NOT inserted → **`limit_size` is still called** (line 150–152 of `process.rs`) → if pool is at capacity, a **second** legitimate tx is evicted unnecessarily.

After both `submit_entry` calls return `Ok(())`, both `_process_tx` invocations return `Some((Ok(verified), snapshot))`, so `after_process` fires twice:
- `send_result_to_relayer(TxVerificationResult::Ok {...})` is called twice → tx is broadcast to peers twice.
- `process_orphan_tx` is called twice → orphan dependents are re-processed and potentially double-relayed.

**Key code references:**

`pre_check` acquires and releases the read lock, performing the only txid-collision check: [1](#0-0) 

`_process_tx` releases the lock between `pre_check` and `submit_entry`, with `verify_rtx` running in the gap: [2](#0-1) 

`submit_entry` does not re-check txid collision, and calls `limit_size` unconditionally after `_submit_entry`: [3](#0-2) 

`pool_map.add_entry` silently returns `(false, evicts)` for a duplicate without error: [4](#0-3) 

`_submit_entry` propagates `Ok(evicts)` even when `succ == false`, so `submit_entry` never sees an error: [5](#0-4) 

The verify queue workers are independent tokio tasks that call `_process_tx` concurrently with the service loop: [6](#0-5) 

### Impact Explanation

**Spurious tx eviction (primary impact):** When the tx pool is at or near its size limit, the second `limit_size` call evicts a legitimate third-party transaction that would not have been evicted without the race. An attacker who controls both an RPC connection and a P2P peer can reliably trigger this by submitting the same valid transaction on both paths simultaneously. The evicted transaction must be re-submitted by its owner, and if the attacker repeats the pattern, they can sustain a low-rate eviction DoS against specific transactions.

**Double relay (secondary impact):** The tx is announced to peers twice via `send_result_to_relayer`, and orphan dependents are processed and potentially relayed twice. This wastes network bandwidth and peer processing resources.

The impact is bounded: one extra eviction per race instance, not unbounded pool corruption. State integrity of the pool itself is preserved because `pool_map.add_entry` is idempotent for duplicates.

### Likelihood Explanation

The race window is the entire duration of `verify_rtx` script execution — up to `max_block_cycles` worth of CKB-VM execution, which can be seconds for complex scripts. Any unprivileged actor who can both call the `send_transaction` RPC and connect as a P2P peer (both are default-open interfaces) can trigger this. No special keys or privileges are required. The attacker simply submits a valid transaction with a high cycle count on both paths at the same time.

### Recommendation

Re-check for txid collision inside `submit_entry` under the write lock, before calling `_submit_entry`. Additionally, guard `limit_size` behind the `succ` flag so it is only called when a transaction was actually inserted:

```rust
// Inside submit_entry's write-lock closure, before _submit_entry:
check_txid_collision(tx_pool, entry.transaction())?;

// After _submit_entry:
let (succ, evicts) = ...; // propagate succ out of _submit_entry
if succ {
    tx_pool.limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
        .map_or(Ok(()), Err)?;
}
```

This mirrors the pattern already applied to `check_rbf`, which the code explicitly notes "must be invoked in `write` lock to avoid concurrent issues." [7](#0-6) 

### Proof of Concept

1. Connect to a CKB node as both an RPC client and a P2P peer.
2. Construct a valid transaction `T` with a high cycle count (e.g., a script that loops near `max_tx_verify_cycles`). Fill the tx pool to near its size limit with lower-fee-rate transactions.
3. Simultaneously:
   - Send `T` via `send_transaction` RPC → triggers `process_tx` → `_process_tx` directly.
   - Relay `T` via P2P `RelayTransaction` message → triggers `resumeble_process_tx` → verify queue worker → `_process_tx`.
4. Both `_process_tx` calls pass `pre_check` (pool does not yet contain `T`).
5. Both run `verify_rtx` concurrently during the long script execution window.
6. Both call `submit_entry`: first inserts `T` and calls `limit_size` (evicts tx A); second finds `T` already present (succ=false) but still calls `limit_size` (evicts tx B).
7. Observe that two legitimate transactions were evicted instead of one.

### Citations

**File:** tx-pool/src/process.rs (L96-170)
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

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

                if !may_recovered_txs.is_empty() {
                    let self_clone = self.clone();
                    tokio::spawn(async move {
                        // push the recovered txs back to verify queue, so that they can be verified and submitted again
                        let mut queue = self_clone.verify_queue.write().await;
                        for tx in may_recovered_txs {
                            debug!("recover back: {:?}", tx.proposal_short_id());
                            let _ = queue.add_tx(tx, false, None);
                        }
                    });
                }
                Ok(())
            })
            .await;

        (ret, snapshot)
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

**File:** tx-pool/src/component/pool_map.rs (L200-221)
```rust
    pub(crate) fn add_entry(
        &mut self,
        mut entry: TxEntry,
        status: Status,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        let tx_short_id = entry.proposal_short_id();
        let mut evicts = Default::default();
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
        Ok((true, evicts))
    }
```

**File:** tx-pool/src/verify_mgr.rs (L147-163)
```rust
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
