Audit Report

## Title
TOCTOU Race in Tx-Pool Admission Allows Redundant Script Verification CPU Exhaustion — (File: `tx-pool/src/process.rs`)

## Summary
The `_process_tx` pipeline performs its duplicate-transaction check (`check_txid_collision`) under a read lock in `pre_check()`, then releases the lock before running expensive script verification in `verify_rtx()`. No in-flight marker is set between the check and the final write-locked `submit_entry()`. Concurrent submissions of the same transaction can each independently pass the duplicate check, each execute full script verification, and only be deduplicated at the write-lock stage — after all redundant CPU work is already done. An unprivileged RPC caller can exploit this to force N full script verifications for a single transaction.

## Finding Description

The three-phase pipeline in `_process_tx` is confirmed by the code:

**Phase 1 — Read-locked check, then released:**
`pre_check()` acquires `with_tx_pool_read_lock`, calls `check_txid_collision` at line 282, and returns. The read lock is dropped before the function returns at line 314. [1](#0-0) [2](#0-1) 

**Phase 2 — Expensive work, no lock held:**
`verify_rtx()` runs full contextual script verification (up to `max_block_cycles` of CPU) with no lock held and no record that this tx is being processed. [3](#0-2) 

**Phase 3 — Write-locked state update:**
`submit_entry()` acquires `with_tx_pool_write_lock` and checks for RBF conflicts (`check_rbf`) and outpoint conflicts (`find_conflict_outpoint`). Critically, it does **not** call `check_txid_collision` under the write lock. A second identical transaction is rejected only because its inputs are already consumed — but only after `verify_rtx()` has already completed for both. [4](#0-3) 

The gap between Phase 1 and Phase 3 spans the entire duration of script verification. Because no in-flight marker exists, N concurrent submissions of the same transaction all pass Phase 1 (pool is empty for all N reads), all execute Phase 2 independently, and N−1 are rejected in Phase 3 only after all N verifications have completed.

The same pattern exists in `resumeble_process_tx`: `orphan_contains` and `verify_queue_contains` are checked with separate, non-atomic lock acquisitions before `enqueue_verify_queue`. Once a tx is dequeued from the verify queue and enters `_process_tx`, `verify_queue_contains` returns false, allowing a concurrent duplicate submission to be enqueued and also enter `verify_rtx()`. [5](#0-4) 

## Impact Explanation

This is a sustained CPU exhaustion attack. The attacker submits the same high-cycle transaction N times concurrently. The node performs N × `max_block_cycles` of wasted script verification CPU work. The verification worker pool can be saturated, delaying or blocking legitimate transaction processing and propagation. A node unable to process and relay transactions in a timely manner contributes to CKB network congestion. The attacker's cost is N cheap RPC calls; the node's cost is N full script verifications — an unbounded amplification ratio controlled entirely by the attacker.

**Impact: High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The attack requires no privileged access, no keys, and no special network position. Any entity that can reach the node's RPC port can submit transactions. Concurrent HTTP submissions are trivially achievable with standard tooling. The vulnerable window is wide — it spans the entire duration of script verification, which for complex scripts can be hundreds of milliseconds. The attack is repeatable indefinitely.

## Recommendation

Re-check for txid collision inside `submit_entry()` under the write lock, before any other work, and return `Reject::Duplicated` immediately if the tx is already present:

```rust
pub(crate) async fn submit_entry(...) {
    self.with_tx_pool_write_lock(move |tx_pool, snapshot| {
        // Authoritative re-check under write lock
        check_txid_collision(tx_pool, entry.transaction())?;
        // ... existing RBF / conflict checks ...
    }).await
}
```

Additionally, maintain an in-flight set (a `HashSet<ProposalShortId>` protected by its own lock) populated atomically at the end of `pre_check()` and cleared at the end of `submit_entry()`, so that concurrent duplicate submissions are rejected before entering the expensive verification phase entirely.

## Proof of Concept

1. Obtain any valid transaction `tx` with scripts that consume close to `max_block_cycles` cycles.
2. Open N concurrent HTTP connections to the node's RPC endpoint.
3. Send `send_transaction(tx)` on all N connections simultaneously.
4. Observe via node metrics or CPU profiling that `verify_rtx` is invoked N times for the same transaction hash.
5. Confirm that only one submission succeeds; N−1 are rejected with a conflict/dead-outpoint error from `submit_entry()`, but only after all N verifications have completed.

The attacker controls N and the script complexity, making the CPU cost arbitrarily large relative to the cost of the attack.

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

**File:** tx-pool/src/process.rs (L276-282)
```rust
        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;
```

**File:** tx-pool/src/process.rs (L344-352)
```rust
        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
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
