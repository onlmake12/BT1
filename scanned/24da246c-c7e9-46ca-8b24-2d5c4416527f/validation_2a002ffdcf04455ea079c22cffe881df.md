Audit Report

## Title
TOCTOU in Tx-Pool Admission: `check_txid_collision` Checked Under Read Lock in `pre_check` but Not Re-Verified Under Write Lock in `submit_entry`, Enabling Concurrent CKB-VM Re-Execution of the Same Transaction — (File: `tx-pool/src/process.rs`)

## Summary
The tx-pool admission pipeline in `_process_tx` performs a duplicate-transaction check (`check_txid_collision`) under a read lock in `pre_check`, releases the lock, runs the expensive CKB-VM verification (`verify_rtx`) with no lock held, and then acquires a write lock in `submit_entry` without repeating the collision check. Because each incoming RPC message is spawned as an independent async task and the RPC path bypasses the verify queue entirely, concurrent `send_transaction` calls with the same transaction can both pass `check_txid_collision` and both trigger full CKB-VM script execution. Only the first submission succeeds at `submit_entry`; the rest are rejected only after the expensive work is already done.

## Finding Description

**Phase 1 — `pre_check` (read lock acquired and released):**
`check_txid_collision` is called inside `with_tx_pool_read_lock` at `process.rs:282`. It checks `tx_pool.contains_proposal_id(&short_id)` and returns `Reject::Duplicated` if the tx is already in the pool. The read lock is dropped when `pre_check` returns.

**Phase 2 — `verify_rtx` (no lock held):**
`verify_rtx` is called at `process.rs:724-732` with no lock held. For the RPC path (`command_rx` is `None`), `util.rs:117` uses `block_in_place`, blocking the current OS thread for the full duration of CKB-VM script execution.

**Phase 3 — `submit_entry` (write lock acquired, no re-check):**
`submit_entry` at `process.rs:96-170` acquires the write lock via `with_tx_pool_write_lock`. The comment at line 104 explicitly acknowledges that `check_rbf` must be inside the write lock to avoid concurrent issues — but `check_txid_collision` is not repeated. The only duplicate protection at this stage is the idempotency guard inside `pool_map.add_entry` at `pool_map.rs:207-208`, which returns `Ok((false, evicts))` silently rather than an error, so the second caller proceeds through `remove_conflict` and `limit_size` even though nothing was inserted.

**Concurrency entry point:**
Each RPC message is spawned as an independent async task at `service.rs:619-622`. The RPC path calls `process_tx` directly (not `resumeble_process_tx`), bypassing the verify queue entirely. The verify queue's deduplication (`add_tx` returning `Ok(false)`) is never reached. The pre-flight checks in `process_tx` at `process.rs:409-411` (`verify_queue_contains`, `orphan_contains`) are separate non-atomic async reads — two concurrent calls both see the tx absent and both proceed to `_process_tx`. Both pass `check_txid_collision` under separate read locks before either reaches `submit_entry`.

## Impact Explanation
An unprivileged RPC caller submitting the same transaction N times concurrently causes N independent CKB-VM executions of the same scripts. For a transaction consuming close to `max_block_cycles` cycles, each concurrent submission blocks an OS thread via `block_in_place` for the full verification duration. Because the RPC path bypasses `max_tx_verify_workers` entirely (it does not go through `VerifyMgr`), the concurrency is bounded only by the number of concurrent RPC connections, not by the worker limit. This can saturate the tokio blocking thread pool, stalling processing of all other pending transactions and degrading node responsiveness. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation
High. The attack requires only the ability to call the public `send_transaction` RPC endpoint multiple times concurrently with the same transaction. No special privileges, keys, or network position are required. The attacker controls the transaction's script complexity (up to `max_block_cycles`) and the concurrency level. The attack is repeatable in a tight loop to sustain thread saturation.

## Recommendation
Re-check `check_txid_collision` inside `submit_entry` under the write lock, immediately before `_submit_entry` is called — mirroring the explicit comment that `check_rbf` must be invoked inside the write lock to avoid concurrent issues:

```rust
// Inside submit_entry's write-lock closure, before _submit_entry:
check_txid_collision(tx_pool, entry.transaction())?;
```

This ensures that if a concurrent submission already inserted the transaction between `pre_check` and `submit_entry`, the second call returns `Reject::Duplicated` immediately without having wasted CKB-VM cycles.

## Proof of Concept
1. Craft a transaction whose lock/type scripts consume close to `max_block_cycles` cycles.
2. Submit the same transaction N times concurrently via `send_transaction` RPC (N ≥ 2).
3. Observe in node logs/metrics that `verify_rtx` is entered N times for the same `tx_hash`.
4. Observe that N−1 submissions are rejected at `submit_entry` (by `check_rbf` or `find_conflict_outpoint`), but only after all N CKB-VM executions complete.
5. Repeat in a tight loop to sustain thread saturation and prevent other transactions from being admitted to the pool. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/process.rs (L276-283)
```rust
        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

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

**File:** tx-pool/src/util.rs (L117-130)
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

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```
