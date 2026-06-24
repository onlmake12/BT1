Audit Report

## Title
TOCTOU Race in Tx-Pool Admission Allows Redundant Script Verification DoS — (File: `tx-pool/src/process.rs`)

## Summary
The `_process_tx` function performs its duplicate-transaction check (`check_txid_collision`) under a read lock in `pre_check()`, then releases the lock before running expensive script verification in `verify_rtx()`. Because `submit_entry()` does not re-check for txid collision under its write lock, concurrent submissions of the same transaction can both pass the duplicate check, both undergo full script verification independently, and only be deduplicated at the final write-locked `submit_entry()` step. An unprivileged RPC caller can exploit this to force N redundant verifications by submitting the same transaction N times concurrently, saturating the verification worker pool.

## Finding Description
The three-phase pipeline in `_process_tx` (L705–753) is:

**Phase 1 — Check (read lock, then released):**
`pre_check()` (L276–316) acquires `with_tx_pool_read_lock`, calls `check_txid_collision` (which tests `tx_pool.contains_proposal_id(&short_id)`), and returns. The read lock is dropped at the end of the closure before the function returns.

**Phase 2 — Expensive work (no lock held):**
`verify_rtx()` (L724–732) runs full contextual script verification — up to `max_block_cycles` of CPU — with no lock held and no record that this tx is being processed.

**Phase 3 — State update (write lock):**
`submit_entry()` (L96–160) acquires `with_tx_pool_write_lock`. Critically, it does **not** call `check_txid_collision` under this write lock. It only checks `check_rbf` (if RBF is enabled) or `find_conflict_outpoint` (if RBF is disabled). For an identical duplicate transaction, `find_conflict_outpoint` will eventually detect the conflict (the inputs are already spent by the first submission), but only after both Phase 2 executions have already completed.

The pre-flight checks in `process_tx` at L409 (`verify_queue_contains` and `orphan_contains`) do not close this window: they are separate async calls that each acquire and release their own locks independently, and neither covers the main tx pool. Two concurrent `process_tx` calls can both pass L409 before either enters `_process_tx`, and then both pass `pre_check()` before either reaches `submit_entry()`.

The `resumeble_process_tx` path (L335–353) has the same structural issue: `orphan_contains` and `verify_queue_contains` are checked with separate, non-atomic lock acquisitions before `enqueue_verify_queue`.

## Impact Explanation
An unprivileged attacker submits the same transaction N times concurrently via the `send_transaction` RPC. Each concurrent call independently passes `check_txid_collision` (the pool is empty for all N reads), then all N calls execute `verify_rtx()` in parallel. Script verification is the most CPU-intensive operation in the node — it runs the CKB-VM for up to `max_block_cycles` cycles per call. With N concurrent submissions, the node performs N × `max_block_cycles` of wasted CPU work. Only after all N verifications complete does `submit_entry()` reject N−1 of them via `find_conflict_outpoint`. This constitutes a sustained CPU exhaustion DoS that can saturate the verification worker pool, delaying or blocking legitimate transaction processing. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points).

## Likelihood Explanation
The attack requires no privileged access, no keys, and no special network position. Any entity that can reach the node's RPC port can submit transactions. The concurrent submission pattern is trivially achievable with standard HTTP clients. The window between Phase 1 and Phase 3 spans the entire duration of script verification, which for complex scripts can be hundreds of milliseconds — a wide and reliable race window. The attacker controls both N (number of concurrent submissions) and the complexity of the transaction's scripts, making the CPU cost arbitrarily large relative to the cost of the attack.

## Recommendation
Re-check for txid collision inside `submit_entry()` under the write lock, **before** any other work, and return `Reject::Duplicated` immediately if the tx is already present:

```rust
pub(crate) async fn submit_entry(...) {
    self.with_tx_pool_write_lock(move |tx_pool, snapshot| {
        // Re-check under write lock — authoritative guard
        check_txid_collision(tx_pool, entry.transaction())?;
        // ... existing RBF / conflict checks ...
    }).await
}
```

Additionally, consider maintaining an in-flight set (a `HashSet<ProposalShortId>` protected by its own lock) that is populated atomically at the end of `pre_check()` and cleared at the end of `submit_entry()`, so that concurrent duplicate submissions are rejected before entering the expensive verification phase.

## Proof of Concept
1. Obtain any valid transaction `tx` that passes non-contextual checks and has non-trivial script verification cost (e.g., a transaction near `max_block_cycles`).
2. Open N concurrent HTTP connections to the node's RPC endpoint.
3. Send `send_transaction(tx)` on all N connections simultaneously.
4. Observe via node metrics or CPU profiling that `verify_rtx` is invoked N times for the same transaction hash, consuming N × script-verification CPU time.
5. Only one submission succeeds; N−1 are rejected with a conflict error from `submit_entry` (via `find_conflict_outpoint` returning `Reject::Resolve(OutPointError::Dead(outpoint))`), but only after all N verifications have completed.

The attacker controls N and the script complexity, making the CPU cost arbitrarily large relative to the cost of the attack.