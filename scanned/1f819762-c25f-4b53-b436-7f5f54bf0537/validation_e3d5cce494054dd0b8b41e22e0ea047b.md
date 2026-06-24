The code confirms the claimed behavior. Here is the validation result:

---

Audit Report

## Title
`OnlySmallCycleTx` Worker Async Busy-Loop via Large-Cycle Tx Flooding — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

## Summary
When the verify queue contains only large-cycle transactions, the `OnlySmallCycleTx` worker enters a tight async loop: `process_inner()` calls `pop_front(true)` which returns `None`, then calls `re_notify()` (storing a `tokio::sync::Notify` permit), and returns. The worker's `run()` loop immediately re-enters `tokio::select!`, finds the stored permit on `queue_ready.notified()`, and calls `process_inner()` again — indefinitely. An unprivileged remote peer can trigger this by flooding the verify queue with transactions whose `declared_cycles` is in the range `(max_tx_verify_cycles, max_block_cycles]`.

## Finding Description

**Attacker entry point.** In `sync/src/relayer/transactions_process.rs`, `TransactionsProcess::execute()` bans a peer only if `declared_cycles > max_block_cycles` (lines 64–74). Transactions with `declared_cycles` in `(large_cycle_threshold, max_block_cycles]` pass through and are enqueued with `is_large_cycle = true` in `verify_queue.rs` (lines 212–214).

**Busy-loop mechanism.** `Worker::run()` loops on `tokio::select!`, waking on `queue_ready.notified()` and calling `process_inner().await` (lines 86–103 of `verify_mgr.rs`). Inside `process_inner()`, the `OnlySmallCycleTx` worker:
1. Acquires a read lock, checks `is_empty()` → `false` (line 120)
2. Acquires a write lock, calls `pop_front(only_small_cycle=true)` → `None` (line 132)
3. Finds `!tasks.is_empty()`, calls `tasks.re_notify()`, and `return`s (lines 135–142)

`re_notify()` calls `self.ready_rx.notify_one()` (line 241 of `verify_queue.rs`). Per tokio semantics, `notify_one()` stores a permit when no task is currently waiting. When `run()` re-enters `tokio::select!` and polls `queue_ready.notified()`, the stored permit causes it to return immediately without suspending. The worker calls `process_inner()` again, which again calls `re_notify()`, storing another permit — and so on.

**Why `SubmitTimeFirst` workers don't break the loop.** `re_notify()` calls `notify_one()`, which wakes exactly one waiter. When `SubmitTimeFirst` workers are busy executing `_process_tx` (which is called outside the lock), they are not waiting on `notified()`. The permit is stored and consumed by `OnlySmallCycleTx` itself on its next `run()` iteration.

**Each iteration cost.** One `read().await` + one `write().await` on an uncontended `RwLock` (both return immediately when uncontended in tokio), plus one `notify_one()`. This makes each iteration extremely fast (sub-microsecond), producing a genuinely tight loop.

## Impact Explanation
The `OnlySmallCycleTx` worker task spins continuously, consuming CPU on the tokio runtime thread it occupies. This degrades throughput of all other async tasks sharing the same runtime — including block relay, peer sync, and RPC handling — for as long as the attacker sustains the flood. The node remains operational but performance-degraded. This matches **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
The attack requires only a valid P2P connection and the ability to send `RelayTransactions` messages with `declared_cycles` in `(max_tx_verify_cycles, max_block_cycles]` (e.g., `70_000_001`). No key material, privileged access, or hashpower is needed. The attacker must sustain a modest stream of fresh transactions to keep the queue non-empty as `SubmitTimeFirst` workers drain it. The 256 MB queue cap limits queue size but not the attack's effectiveness — the queue only needs to remain non-empty.

## Recommendation
In `process_inner()`, when `pop_front(only_small_cycle=true)` returns `None` but the queue is non-empty (all large-cycle), the worker must yield to the scheduler before returning. Options:
- Insert `tokio::task::yield_now().await` before the `return` in the `None` branch, ensuring at least one scheduler tick between iterations.
- Replace `tasks.re_notify()` + immediate `return` with a short `tokio::time::sleep(Duration::from_millis(1)).await`, preventing the tight loop.
- Use a separate `Notify` for large-cycle vs. small-cycle events so `OnlySmallCycleTx` is not woken by large-cycle-only notifications.

## Proof of Concept
```
1. Connect to a CKB node as a P2P peer (RelayV3 protocol).
2. Announce N distinct tx hashes via RelayTransactionHashes.
3. When the node sends GetRelayTransactions, respond with RelayTransactions
   where each tx has declared_cycles = max_tx_verify_cycles + 1 (e.g., 70_000_001).
4. Repeat with fresh tx hashes to keep the verify queue non-empty.
5. Observe: the OnlySmallCycleTx worker (worker_id=0) consumes elevated CPU
   continuously, measurable via tokio-console or /proc/<pid>/task/<tid>/stat,
   while no small-cycle txs are present in the queue.
```