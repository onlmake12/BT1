Audit Report

## Title
TOCTOU Race in Tx-Pool Admission Allows Redundant Script Verification DoS — (File: `tx-pool/src/process.rs`)

## Summary
The `_process_tx` function performs its duplicate-transaction check (`check_txid_collision`) under a read lock in `pre_check()`, then releases the lock before running expensive script verification in `verify_rtx()`. No in-flight marker is set between the check and the state update, so N concurrent submissions of the same transaction all pass the duplicate check, all execute full script verification independently, and are only deduplicated at the final write-locked `submit_entry()` step — after all N verifications have already completed. This allows an unprivileged attacker to force N × `max_block_cycles` of wasted CPU work with only N cheap RPC calls.

## Finding Description
The three-phase pipeline in `_process_tx` (lines 705–777 of `tx-pool/src/process.rs`) is:

**Phase 1 — `pre_check()` (read lock, then released):**
`pre_check()` acquires `with_tx_pool_read_lock`, calls `check_txid_collision`, and returns. The lock is dropped before returning.

```rust
// process.rs L276-282
let (ret, snapshot) = self
    .with_tx_pool_read_lock(|tx_pool, snapshot| {
        ...
        check_txid_collision(tx_pool, tx)?;  // ← only check, no marker set
        ...
    })
    .await;
```

`check_txid_collision` simply tests `tx_pool.contains_proposal_id(&short_id)` (`util.rs` L20–26). If the pool is empty, all N concurrent callers pass this check simultaneously.

**Phase 2 — `verify_rtx()` (no lock held):**
Full contextual script verification runs with no lock held and no record that this tx is being processed (`process.rs` L724–732). For a complex transaction this can consume up to `max_block_cycles` of CPU per call.

**Phase 3 — `submit_entry()` (write lock):**
`submit_entry()` acquires `with_tx_pool_write_lock` and checks for conflicts via `check_rbf` or `find_conflict_outpoint` (`process.rs` L103–116). It does **not** call `check_txid_collision` again. The second identical tx is rejected here because its inputs are now spent by the first — but only after both have already completed Phase 2.

The gap between Phase 1 and Phase 3 is the vulnerable window. Because no "being-processed" marker exists, N concurrent requests for the same transaction all pass Phase 1 (the pool is empty for all N reads), all execute Phase 2 independently, and N−1 are rejected only in Phase 3.

The same pattern exists in `resumeble_process_tx` (L344–352), where `orphan_contains` and `verify_queue_contains` are checked with separate, non-atomic lock acquisitions before `enqueue_verify_queue`, creating additional TOCTOU windows.

## Impact Explanation
This is a **sustained CPU exhaustion DoS** matching the allowed High impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* The attacker pays only the cost of N RPC calls; the node pays N full script verifications. A single high-cycle transaction (near the block cycle limit) submitted dozens of times concurrently can saturate the verification worker pool, delaying or blocking legitimate transaction processing across the node. Because the rejected transaction's inputs remain unspent, the attacker can repeat the attack indefinitely with the same transaction.

## Likelihood Explanation
The attack requires no privileged access, no keys beyond owning a single valid unspent cell, and no special network position. Any entity that can reach the node's RPC port can submit transactions. Concurrent submission is trivially achievable with standard HTTP clients. The vulnerable window spans the entire duration of script verification — potentially hundreds of milliseconds for complex scripts — making the race easy to win reliably.

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
1. Obtain any valid transaction `tx` that passes non-contextual checks (requires owning one unspent CKB cell).
2. Open N concurrent HTTP connections to the node's RPC endpoint.
3. Send `send_transaction(tx)` on all N connections simultaneously.
4. Observe via node metrics or CPU profiling that `verify_rtx` is invoked N times for the same transaction hash, consuming N × script-verification CPU time.
5. Only one submission succeeds; N−1 are rejected with a conflict error from `submit_entry`, but only after all N verifications have completed.
6. Because the rejected transaction's inputs remain unspent, repeat from step 3 indefinitely to sustain the DoS.