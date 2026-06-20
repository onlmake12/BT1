### Title
TOCTOU Race in `_process_tx` Violates Check-Interaction-Effect Ordering — (`tx-pool/src/process.rs`)

### Summary

`TxPoolService::_process_tx` performs its duplicate-admission check under a **read lock**, releases that lock, runs expensive script verification with **no lock held**, and then acquires a **write lock** to insert the transaction. The write-lock phase does not re-check for txid collision when the chain tip is unchanged. Two concurrent callers submitting the same transaction can both pass the read-lock check, both execute the full `verify_rtx` pipeline, and then the second writer's failure path incorrectly records a valid, already-accepted transaction in the conflicts cache — a state inconsistency analogous to the CEI violation in the reference report.

---

### Finding Description

`_process_tx` in `tx-pool/src/process.rs` follows this sequence:

```
1. pre_check()        — read lock on tx_pool acquired and released
2. verify_rtx()       — NO lock held (expensive async script execution)
3. submit_entry()     — write lock on tx_pool acquired
``` [1](#0-0) 

Inside `pre_check`, `check_txid_collision` verifies the transaction is not already in the pool — but only while the read lock is held. [2](#0-1) 

Inside `submit_entry`, when `pre_resolve_tip == tip_hash` (tip has not changed between check and effect), the code skips the time-relative re-verification block and goes directly to `process_rbf` → `_submit_entry` **without re-checking for txid collision**. [3](#0-2) 

When RBF is disabled, `submit_entry` calls `find_conflict_outpoint` to detect conflicts. If the same transaction was already inserted by a concurrent first caller, `find_conflict_outpoint` returns `Some` (the tx's own inputs are already registered in `edges.inputs`), causing `submit_entry` to return `Err(Reject::Resolve(OutPointError::Dead(...)))`. [4](#0-3) 

`after_process` then matches on `Reject::Resolve(OutPointError::Dead(_))` and calls `record_conflict` on the transaction — even though it is valid and already accepted in the pending pool. [5](#0-4) 

The `Edges::insert_input` double-spend guard, which would produce `RBFRejected`, is the other failure path for the same scenario. [6](#0-5) 

A concrete concurrent path exists because the service loop spawns each incoming message as an independent Tokio task: [7](#0-6) 

`process_tx` (used by the local `send_transaction` RPC) calls `_process_tx` directly, bypassing the verify queue. Its duplicate guard checks `verify_queue_contains` and `orphan_contains`, but **not** whether `_process_tx` is already executing for the same tx in another task: [8](#0-7) 

The verify queue worker also calls `_process_tx` directly after popping an entry — at which point the tx is no longer visible to `verify_queue_contains`: [9](#0-8) 

---

### Impact Explanation

1. **CPU exhaustion**: Both concurrent callers execute the full `verify_rtx` pipeline (RISC-V script execution, potentially up to `max_block_cycles`). A malicious actor submitting the same large-cycle transaction repeatedly via concurrent RPC calls forces redundant verification work proportional to the number of concurrent submissions.

2. **State inconsistency — valid tx in conflicts cache**: The second caller's `after_process` invokes `record_conflict`, inserting the valid, pool-accepted transaction into `conflicts_cache` and `conflicts_outputs_cache`. The `conflicts_outputs_cache` maps outpoints to short IDs and is consumed by `get_conflicted_txs_from_inputs` inside `process_rbf`. If the accepted transaction is later evicted from the pending pool (e.g., by `limit_size`), a subsequent RBF replacement attempt for the same inputs may incorrectly treat the evicted-but-conflict-cached transaction as a recoverable conflict, re-queuing it into the verify queue and causing further incorrect processing. [10](#0-9) 

---

### Likelihood Explanation

The race window is the duration of `verify_rtx`, which for large-cycle scripts can be hundreds of milliseconds. Two realistic triggers:

- A user submits a transaction via `send_transaction` RPC while the same transaction relayed by a peer is being processed by the verify queue worker (the worker pops the tx, making it invisible to `verify_queue_contains`, then the RPC call proceeds).
- Two concurrent `send_transaction` RPC calls for the same transaction (e.g., from a wallet retrying on timeout).

Both are reachable by an unprivileged RPC caller or network peer with no special privileges.

---

### Recommendation

Re-check for txid collision inside `submit_entry` under the write lock, regardless of whether the tip hash changed. A minimal fix is to call `check_txid_collision` (or an equivalent `pool_map.get_by_id` lookup) at the start of the write-lock closure in `submit_entry`, before `process_rbf` and `_submit_entry` are invoked. This mirrors the CEI fix: move the "already-accepted" state check to occur atomically with the insertion effect, eliminating the window between the read-lock check and the write-lock insertion.

---

### Proof of Concept

**Setup**: Node with RBF disabled (`min_rbf_rate == min_fee_rate`).

**Steps**:

1. Craft a transaction `T` with a script that consumes close to `max_tx_verify_cycles` cycles (ensuring `verify_rtx` takes significant wall time).
2. Submit `T` via two concurrent `send_

### Citations

**File:** tx-pool/src/process.rs (L107-116)
```rust
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

**File:** tx-pool/src/process.rs (L188-235)
```rust
    // try to remove conflicted tx here, the returned txs can be re-verified and re-submitted
    // since they maybe not conflicted anymore
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
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
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
    }
```

**File:** tx-pool/src/process.rs (L276-316)
```rust
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

**File:** tx-pool/src/process.rs (L479-487)
```rust
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

**File:** tx-pool/src/process.rs (L715-753)
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

**File:** tx-pool/src/service.rs (L616-632)
```rust
        self.handle.spawn(async move {
            loop {
                tokio::select! {
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
                    },
                    _ = signal_receiver.cancelled() => {
                        info!("TxPool is saving, please wait...");
                        process_service.save_pool().await;
                        info!("TxPool process_service exit now");
                        break
                    },
                    else => break,
                }
            }
        });
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
