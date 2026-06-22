### Title
TOCTOU in Tx-Pool Admission: `check_txid_collision` Checked Under Read Lock in `pre_check` but Not Re-Verified Under Write Lock in `submit_entry`, Enabling Concurrent CKB-VM Re-Execution of the Same Transaction — (File: `tx-pool/src/process.rs`)

---

### Summary

The tx-pool admission pipeline in `_process_tx` performs a duplicate-transaction check (`check_txid_collision`) under a **read lock** in `pre_check`, then releases the lock for the long-running CKB-VM verification (`verify_rtx`), and finally acquires a **write lock** in `submit_entry` without re-checking for duplicates. Because each incoming RPC message is spawned as an independent async task, concurrent `send_transaction` RPC calls with the same transaction can both pass the read-lock check and both trigger full CKB-VM script execution before either reaches the write lock. This is the structural analog to the 1inch reentrancy bug: state is checked, the guard is released, an external operation runs, and the guard is re-acquired without repeating the check.

---

### Finding Description

The three-phase pipeline in `_process_tx` is:

**Phase 1 — `pre_check` (read lock acquired and released):** [1](#0-0) 

`check_txid_collision` is called here under the read lock. It checks `tx_pool.contains_proposal_id(&short_id)` and returns `Reject::Duplicated` if the tx is already in the pool. [2](#0-1) 

After `pre_check` returns, the read lock is dropped.

**Phase 2 — `verify_rtx` (no lock held):** [3](#0-2) 

This is the expensive CKB-VM script execution. For the local/RPC path (`command_rx` is `None`), it calls `block_in_place`, blocking the current OS thread for the full duration of script execution. [4](#0-3) 

**Phase 3 — `submit_entry` (write lock acquired):** [5](#0-4) 

The write lock is acquired here. The comment explicitly acknowledges that `check_rbf` must be inside the write lock to avoid concurrent issues. However, `check_txid_collision` is **not repeated** here. The only duplicate protection at this stage is the idempotency guard inside `pool_map.add_entry`: [6](#0-5) 

This returns `(false, evicts)` silently — not an error — so the caller proceeds to `remove_conflict` and `limit_size` even though nothing was inserted.

**The concurrency entry point:**

Each message received from the RPC channel is spawned as an independent async task: [7](#0-6) 

So two concurrent `send_transaction` RPC calls with the same transaction produce two concurrent `process_tx` invocations. The pre-flight checks in `process_tx` are also done under separate, non-atomic read locks: [8](#0-7) 

Both calls can pass all three checks (`verify_queue_contains`, `orphan_contains`, `check_txid_collision`) before either reaches `submit_entry`.

The `VerifyMgr` spawns up to `max_tx_verify_workers` (default: `3/4 * num_cpus`) concurrent workers, each calling `_process_tx`: [9](#0-8) [10](#0-9) 

---

### Impact Explanation

An unprivileged RPC caller submitting the same transaction N times concurrently causes N independent CKB-VM executions of the same scripts. For a transaction using the maximum allowed cycles (`max_block_cycles`), this multiplies CPU consumption by N. Because `verify_rtx` uses `block_in_place` on the local/RPC path, each concurrent submission blocks an OS thread for the full verification duration. On an 8-core node (`max_tx_verify_workers = 6`), six concurrent submissions of a max-cycle transaction can saturate all verification threads, stalling processing of all other pending transactions. Only the first submission succeeds; the rest are rejected at `submit_entry` by `check_rbf` or `find_conflict_outpoint` — but only after the expensive CKB-VM work is already done.

---

### Likelihood Explanation

High. The attack requires only the ability to call the public `send_transaction` RPC endpoint multiple times concurrently with the same transaction. No special privileges, keys, or network position are required. The attacker controls the transaction's script complexity (up to `max_block_cycles`) and the concurrency level.

---

### Recommendation

Re-check `check_txid_collision` inside `submit_entry` under the write lock, immediately before `_submit_entry` is called — mirroring the explicit comment that `check_rbf` must be invoked inside the write lock to avoid concurrent issues:

```rust
// Inside submit_entry's write-lock closure, before _submit_entry:
check_txid_collision(tx_pool, entry.transaction())?;
```

This ensures that if a concurrent submission already inserted the transaction between `pre_check` and `submit_entry`, the second call returns `Reject::Duplicated` immediately without having wasted CKB-VM cycles.

---

### Proof of Concept

1. Craft a transaction whose lock/type scripts consume close to `max_block_cycles` cycles.
2. Submit the same transaction N times concurrently via `send_transaction` RPC (N ≥ 2).
3. Observe in node logs/metrics that `verify_rtx` is entered N times for the same `tx_hash`.
4. Observe that N−1 submissions are rejected at `submit_entry` (by `check_rbf` or `find_conflict_outpoint`), but only after all N CKB-VM executions complete.
5. Repeat in a tight loop to sustain thread saturation and prevent other transactions from being admitted to the pool.

The root cause is the lock-release gap between `pre_check` (read lock, `check_txid_collision`) and `submit_entry` (write lock, no `check_txid_collision`), with the expensive `verify_rtx` executing in the unguarded window — a structural TOCTOU directly analogous to the 1inch reentrancy pattern where state is checked, the guard is released, an external operation executes, and the guard is re-acquired without repeating the check.

### Citations

**File:** tx-pool/src/process.rs (L102-116)
```rust
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

**File:** tx-pool/src/process.rs (L276-314)
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
```

**File:** tx-pool/src/process.rs (L409-411)
```rust
        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
```

**File:** tx-pool/src/process.rs (L724-732)
```rust
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

**File:** tx-pool/src/util.rs (L117-131)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L207-209)
```rust
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
```

**File:** tx-pool/src/service.rs (L619-622)
```rust
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
                    },
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

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```
