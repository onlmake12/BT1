The code confirms all technical claims. Let me verify the remaining detail about `verify_queue_contains`.

Audit Report

## Title
TOCTOU Race in `_process_tx` Enables Wasted Concurrent Script Verification of Conflicting Transactions — (File: `tx-pool/src/process.rs`)

## Summary
`_process_tx` performs a cell-liveness check under a read lock in `pre_check`, releases the lock, then runs full `ContextualTransactionVerifier` script execution in `verify_rtx` with no lock held. Because multiple concurrent readers can hold the read lock simultaneously, N transactions all spending the same live cell can each pass `pre_check`, proceed to full script verification concurrently, and only be rejected at `submit_entry` under the write lock — wasting N-1 full verifications per batch.

## Finding Description

**Step 1 — `pre_check` under read lock:**
`pre_check` calls `with_tx_pool_read_lock` at line 277, which acquires `self.tx_pool.read().await` (line 251). Multiple concurrent callers all hold the read lock simultaneously. Inside the closure, `resolve_tx(tx_pool, &snapshot, tx.clone(), false)` at line 286 checks whether input cells are live. If cell `C` has not yet been consumed by any pool entry, all N concurrent callers see it as live and return `Ok(...)`.

**Step 2 — `verify_rtx` with no lock held:**
After `pre_check` returns and the read lock is dropped, `_process_tx` calls `verify_rtx` at lines 724–732 with no lock held. This runs the full `ContextualTransactionVerifier` — the most expensive operation in the pipeline. All N callers execute this concurrently.

**Step 3 — `submit_entry` under write lock detects conflict too late:**
Only at `submit_entry` (line 103, `with_tx_pool_write_lock`) does the conflict check fire. With RBF disabled, `tx_pool.pool_map.find_conflict_outpoint(entry.transaction())` at lines 110–114 detects the double-spend and returns `Err(Reject::Resolve(OutPointError::Dead(outpoint)))` for T2…TN. T1 succeeds; N-1 full script verifications were entirely wasted.

**Multiple concurrent workers confirmed:**
`VerifyMgr` spawns `max_tx_verify_workers` workers at lines 179–180. Each worker independently pops an entry from the shared queue under a write lock (lines 130–145) and calls `_process_tx` (lines 147–154), meaning multiple workers process different queue entries concurrently with no coordination between them.

**Deduplication does not prevent this:**
`resumeble_process_tx` at lines 349–351 checks `verify_queue_contains` by transaction identity (same tx hash). N distinct transactions spending the same cell have N distinct hashes — all pass the deduplication check and are all enqueued.

**Design explicitly acknowledged:**
The comment at lines 305–307 explicitly states that conflicting transactions are allowed to proceed through verification before `submit_entry`. This is intentional for the RBF path but equally applies to N fresh transactions all spending the same live cell — none of which is yet in the pool — so all take the `Ok` branch in `pre_check`.

## Impact Explanation
An attacker controlling one live cell with a high-cycle lock script can construct N transactions spending that cell. All N enter the verify queue (each has a unique hash), are dequeued by `max_tx_verify_workers` workers concurrently, each runs full script execution up to `max_tx_verify_cycles`, and N-1 are rejected only at `submit_entry`. The attacker can sustain the attack across multiple cells or by chaining outputs, continuously saturating all verify workers. This delays block template generation and legitimate transaction processing. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attack requires no privileged access. Any P2P peer can relay transactions. Constructing N valid transactions spending the same cell requires only ownership of that cell and a valid lock script. The verify queue size limit and per-transaction cycle cap bound damage per batch, but the attacker can use multiple cells or chain outputs to replenish. The attack is repeatable and requires no majority hashpower or special protocol knowledge.

## Recommendation
Apply a "pending inputs" reservation before releasing the read lock:

1. **Preferred:** Introduce a `pending_inputs: HashSet<OutPoint>` set in `TxPool`. Upgrade `pre_check` to use a write lock (or add a separate write-lock step) to atomically check and insert the transaction's input outpoints into `pending_inputs` before releasing. Remove them in `submit_entry` on success or failure.
2. **Alternative:** Upgrade `pre_check` to use `with_tx_pool_write_lock` entirely, accepting serialized pre-checks.
3. **Secondary:** Combine the orphan check, verify-queue check, and enqueue in `resumeble_process_tx` into a single write-lock section to eliminate the check-then-enqueue window.

## Proof of Concept
1. Attacker owns live cell `C` with a lock script that loops near `max_tx_verify_cycles`.
2. Attacker constructs transactions T1…TN, each spending cell `C` with different outputs (distinct tx hashes).
3. Attacker's peers P1…PN simultaneously relay T1…TN via the transaction relay protocol, triggering `submit_remote_tx` → `resumeble_process_tx` → `enqueue_verify_queue` for each.
4. All N transactions enter the verify queue (each has a unique hash, queue size limit not yet exceeded).
5. `max_tx_verify_workers` workers each pop an entry and call `_process_tx` concurrently.
6. All N calls invoke `pre_check` under the read lock — cell `C` appears live to all (none has reached `submit_entry` yet). All N return `Ok`.
7. All N calls enter `verify_rtx` concurrently with no lock held, each running full script execution.
8. All N calls reach `submit_entry` (write lock). T1 is accepted; T2…TN are rejected with `Reject::Resolve(OutPointError::Dead(C))`.
9. N-1 full script verifications were wasted. Attacker repeats with a new cell or T1's output, continuously saturating all verify workers.