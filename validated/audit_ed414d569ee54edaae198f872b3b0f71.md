Audit Report

## Title
TOCTOU Race in `_process_tx` Enables Redundant CKB-VM Script Verification via Concurrent Duplicate Submissions — (`tx-pool/src/process.rs`)

## Summary

`_process_tx` performs the duplicate-transaction check (`check_txid_collision`) under a read lock in `pre_check`, releases all locks, runs expensive CKB-VM script verification (`verify_rtx`) with no lock held, then re-acquires a write lock in `submit_entry` without re-checking the duplicate condition. An attacker can exploit this window to force multiple full CKB-VM executions for the same transaction, exhausting CPU resources. A secondary effect pollutes the `conflicts_pool` LRU cache with valid transactions.

## Finding Description

The transaction admission pipeline in `_process_tx` (lines 705–777) follows a check-then-act pattern that is not atomic:

**Step 1 — Check (read lock acquired and released):**
`pre_check` (lines 269–316) calls `with_tx_pool_read_lock`, which acquires `tx_pool.read()`, calls `check_txid_collision` (util.rs lines 20–26) to reject duplicates by `ProposalShortId`, then drops the read lock before returning.

**Step 2 — Interaction (no lock held):**
`verify_rtx` (lines 724–732 of `_process_tx`) runs the full `ContextualTransactionVerifier` (CKB-VM script execution) with no lock held. For a max-cycles transaction this window spans hundreds of milliseconds.

**Step 3 — Effect (write lock acquired, no re-check):**
`submit_entry` (lines 96–170) acquires `tx_pool.write()`. It re-checks for conflicting inputs via `find_conflict_outpoint`/`check_rbf` (the comment at line 104 explicitly notes this must be inside the write lock to avoid concurrent issues), but **never re-calls `check_txid_collision`**. There is no guard against a duplicate that passed `pre_check` while the first copy was in `verify_rtx`.

**Exploit window:**
1. Submission A of transaction T is dequeued from `verify_queue` and enters `_process_tx`. It passes `pre_check` (T not yet in pool) and begins `verify_rtx`.
2. While A is inside `verify_rtx` (no lock held, T not in pool, T not in verify_queue), submission B of the same T arrives. It passes `verify_queue_contains` (T was dequeued), gets enqueued by `enqueue_verify_queue` (lines 860–868; `add_tx` at verify_queue.rs lines 198–237 only prevents duplicates already in the queue, not ones currently being verified), and is dequeued for processing.
3. B passes `pre_check` (T still not in pool — A has not yet reached `submit_entry`). B begins `verify_rtx` concurrently with A.
4. A completes `submit_entry` first, inserting T into the pool. B reaches `submit_entry`, finds T's inputs already spent by T in the pool, and `find_conflict_outpoint` returns `Reject::Resolve(OutPointError::Dead(...))`.
5. `after_process` (lines 479–487) matches `Err(Reject::Resolve(OutPointError::Dead(_)))`, calls `find_conflict_outpoint` again (returns Some, since T is in pool), and calls `record_conflict(tx)` — inserting the valid duplicate T into `conflicts_pool`, polluting the LRU conflict cache.

The `add_tx` duplicate check inside `enqueue_verify_queue` (verify_queue.rs lines 204–209) only prevents two copies from sitting in the queue simultaneously; it does not protect the window between dequeue and `submit_entry`, which is where the TOCTOU lives.

## Impact Explanation

**High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker with a transaction whose lock script consumes close to `max_block_cycles` can repeatedly force N full CKB-VM executions (one per submission timed to the verification window) at the cost of sending N P2P relay messages. Each wasted execution consumes CPU proportional to `max_block_cycles`. Sustained attack across multiple transactions degrades node throughput for legitimate transactions, contributing to network-wide congestion. The secondary `conflicts_pool` pollution evicts legitimate conflict records from the LRU cache, degrading RBF conflict tracking.

## Likelihood Explanation

**Medium.** Any unprivileged peer can submit transactions via the P2P relay protocol (`RelayTransactions`). The TOCTOU window spans the entire CKB-VM execution time for the first copy — potentially hundreds of milliseconds for max-cycles scripts. An attacker needs only to time a second submission to arrive after the first is dequeued but before it completes `submit_entry`. This is achievable with a standard CKB client by sending the same transaction twice in rapid succession. No special privileges, keys, or majority hashpower are required. The attack is repeatable indefinitely.

## Recommendation

Re-check `check_txid_collision` (or equivalently `contains_proposal_id`) **inside `submit_entry` under the write lock**, before calling `_submit_entry`. This mirrors the existing pattern where `check_rbf` is explicitly placed inside the write lock (line 104–106) to prevent concurrent issues:

```rust
// Inside submit_entry's write-lock closure, before _submit_entry:
if tx_pool.contains_proposal_id(&entry.proposal_short_id()) {
    return Err(Reject::Duplicated(entry.transaction().hash()));
}
```

This ensures the duplicate check and the pool insertion are atomic with respect to concurrent submissions.

## Proof of Concept

1. Construct a valid transaction T whose lock script consumes close to `max_block_cycles` cycles (e.g., a tight loop in CKB-VM).
2. Open a P2P connection to the target node and send T via `RelayTransactions`. Wait for T to be dequeued from `verify_queue` (observable via node logs or timing).
3. Immediately send T again via `RelayTransactions` while the first verification is still running (within the `verify_rtx` window).
4. Observe node logs: both submissions log entry into `_process_tx` and both call `verify_rtx`. Only one succeeds in `submit_entry`; the second is rejected with `Reject::Resolve(OutPointError::Dead(...))`.
5. Confirm `conflicts_pool` pollution: query the node's conflict cache and observe T present despite being a valid (non-conflicting) transaction.
6. Repeat steps 2–5 in a loop to sustain CPU exhaustion. Each iteration forces one extra full CKB-VM execution at the cost of one relay message.