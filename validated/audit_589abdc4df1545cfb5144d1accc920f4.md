### Title
TOCTOU Race in Tx-Pool Admission Allows Redundant Script Verification DoS — (File: `tx-pool/src/process.rs`)

---

### Summary

The tx-pool's `_process_tx` function performs its duplicate-transaction check (`check_txid_collision`) under a **read lock** in `pre_check()`, then releases the lock before running expensive script verification in `verify_rtx()`. Because no "in-flight" marker is set between the check and the state update, concurrent submissions of the same transaction can both pass the duplicate check, both undergo full script verification (up to `max_block_cycles` CPU cycles each), and only be deduplicated at the final write-locked `submit_entry()` step. An unprivileged RPC caller can exploit this to force N redundant verifications by submitting the same transaction N times concurrently.

---

### Finding Description

The three-phase pipeline in `_process_tx` is:

**Phase 1 — Check (read lock, then released):**
`pre_check()` acquires `with_tx_pool_read_lock`, calls `check_txid_collision`, and returns. The lock is dropped before returning. [1](#0-0) 

```rust
let (ret, snapshot) = self
    .with_tx_pool_read_lock(|tx_pool, snapshot| {
        ...
        check_txid_collision(tx_pool, tx)?;   // ← only check, no marker set
        ...
    })
    .await;
```

`check_txid_collision` simply tests `tx_pool.contains_proposal_id(&short_id)`: [2](#0-1) 

**Phase 2 — Expensive work (no lock held):**
`verify_rtx()` runs full contextual script verification — up to `max_block_cycles` of CPU — with **no lock held** and no record that this tx is being processed. [3](#0-2) 

**Phase 3 — State update (write lock):**
`submit_entry()` acquires `with_tx_pool_write_lock` and inserts the entry. Only here is a conflict check re-run (via `find_conflict_outpoint` or `check_rbf`), which will reject the second identical tx — but only after both have already completed Phase 2. [4](#0-3) 

The gap between Phase 1 and Phase 3 is the vulnerable window. Because no "being-processed" marker exists, two concurrent requests for the same transaction both pass Phase 1 (the pool is empty for both reads), both execute Phase 2 independently, and only the second is rejected in Phase 3.

The same pattern exists in the async entry path `resumeble_process_tx`, where `orphan_contains` and `verify_queue_contains` are checked with **separate, non-atomic lock acquisitions** before `enqueue_verify_queue`: [5](#0-4) 

Each check releases its lock before the next is acquired, creating additional TOCTOU windows.

---

### Impact Explanation

An unprivileged attacker submits the same transaction N times concurrently via the `send_transaction` RPC. Each concurrent call independently passes `check_txid_collision` (the pool is empty for all N reads), then all N calls execute `verify_rtx()` in parallel. Script verification is the most CPU-intensive operation in the node — it runs the CKB-VM for up to `max_block_cycles` cycles per call. With N concurrent submissions, the node performs N × `max_block_cycles` of wasted CPU work. Only after all N verifications complete does `submit_entry()` reject N−1 of them.

This is a **sustained CPU exhaustion DoS**: the attacker pays only the cost of N RPC calls; the node pays N full script verifications. A single high-cycle transaction (e.g., one near the block cycle limit) submitted dozens of times concurrently can saturate the verification worker pool, delaying or blocking legitimate transaction processing.

---

### Likelihood Explanation

The attack requires no privileged access, no keys, and no special network position. Any entity that can reach the node's RPC port (default: open to localhost, often exposed to LAN or internet in practice) can submit transactions. The concurrent submission pattern is trivially achievable with standard HTTP clients. The window between Phase 1 and Phase 3 is wide — it spans the entire duration of script verification, which for complex scripts can be hundreds of milliseconds.

---

### Recommendation

Re-check for txid collision inside `submit_entry()` under the write lock, **before** any other work, and return `Reject::Duplicated` immediately if the tx is already present. This mirrors the recommended fix in the original report: move the authoritative state check to occur atomically with the state update.

```rust
pub(crate) async fn submit_entry(...) {
    self.with_tx_pool_write_lock(move |tx_pool, snapshot| {
        // Re-check under write lock — authoritative guard
        check_txid_collision(tx_pool, entry.transaction())?;
        // ... existing RBF / conflict checks ...
    }).await
}
```

Additionally, consider using an in-flight set (a `HashSet<ProposalShortId>` protected by its own lock) that is populated atomically at the end of `pre_check()` and cleared at the end of `submit_entry()`, so that concurrent duplicate submissions are rejected before entering the expensive verification phase.

---

### Proof of Concept

1. Obtain any valid transaction `tx` that passes non-contextual checks.
2. Open N concurrent HTTP connections to the node's RPC endpoint.
3. Send `send_transaction(tx)` on all N connections simultaneously.
4. Observe via node metrics or CPU profiling that `verify_rtx` is invoked N times for the same transaction hash, consuming N × script-verification CPU time.
5. Only one submission succeeds; N−1 are rejected with a conflict error from `submit_entry`, but only after all N verifications have completed.

The attacker controls N and the complexity of the transaction's scripts, making the CPU cost arbitrarily large relative to the cost of the attack.

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
