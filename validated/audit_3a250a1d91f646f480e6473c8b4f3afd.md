Audit Report

## Title
OnlySmallCycleTx Worker Busy-Wait with O(N) Write-Lock-Held Scan via Large-Cycle Tx Flooding — (`tx-pool/src/component/verify_queue.rs`)

## Summary

When `max_tx_verify_workers > 1` (the default on any multi-core node), worker 0 is assigned `WorkerRole::OnlySmallCycleTx`. An attacker who floods the verify queue exclusively with transactions whose peer-declared cycle count exceeds `large_cycle_threshold` causes this worker to enter a persistent busy-wait loop: it wakes up, acquires the queue write lock, performs an O(N) linear scan of the entire queue finding nothing, calls `re_notify()` which immediately re-wakes itself, and repeats — holding the write lock during each scan and blocking `SubmitTimeFirst` workers from dequeuing legitimate transactions.

## Finding Description

**Root cause 1 — O(N) scan instead of O(1) index lookup:**

`peek(only_small_cycle=true)` at `verify_queue.rs:187-188` uses:
```rust
self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
```
A `#[multi_index(hashed_non_unique)]` index on `is_large_cycle` is declared at lines 46-47 and would allow an O(1) existence check, but it is never used in `peek()`. With N large-cycle txs in the queue, this is O(N) per call.

**Root cause 2 — Busy-wait loop via `re_notify()` self-wakeup:**

In `verify_mgr.rs:130-143`, when `pop_front(true)` returns `None` but the queue is non-empty, the worker calls `tasks.re_notify()` and returns. `re_notify()` calls `tokio::sync::Notify::notify_one()` (`verify_queue.rs:241`), which stores a permit. Back in `run()` (`verify_mgr.rs:98-99`), the worker immediately re-enters `process_inner()` by consuming the stored permit from `queue_ready.notified()` without sleeping. When all `SubmitTimeFirst` workers are busy processing large-cycle txs inside their own `process_inner()` loop, they are not waiting on `queue_ready.notified()`, so `notify_one()` always re-wakes the `OnlySmallCycleTx` worker, creating a tight cooperative loop.

**Root cause 3 — Write lock held during O(N) scan:**

`pop_front()` is called while holding the queue write lock (`verify_mgr.rs:131-132`). The write lock is held for the entire duration of the O(N) `peek()` scan. During this time, `SubmitTimeFirst` workers cannot acquire the write lock to dequeue their own entries.

**Attacker entry point:**

The `is_large_cycle` flag is set purely from the peer-declared cycle count (`verify_queue.rs:212-214`):
```rust
let is_large_cycle = remote
    .map(|(cycles, _)| cycles > self.large_cycle_threshold)
    .unwrap_or(false);
```
A remote peer can declare any cycle count. Transactions only need to pass `non_contextual_verify` (structural checks) before being enqueued via `resumeble_process_tx` (`process.rs:335-352`). No valid scripts are required.

**Default configuration activates the vulnerable path:**

`default_max_tx_verify_workers()` returns `max(num_cpus * 3/4, 1)` (`tx_pool.rs:46-47`), which is > 1 on any multi-core machine. Worker 0 is assigned `OnlySmallCycleTx` whenever `worker_num > 1` (`verify_mgr.rs:185-188`).

## Impact Explanation

This matches **High — "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."** The attacker sends structurally valid, minimum-size transactions with declared cycles above `large_cycle_threshold`. The queue is bounded at 256 MB (`verify_queue.rs:18`), so the attacker can pack a large number of transactions. Each busy-wait iteration holds the write lock for an O(N) scan, starving `SubmitTimeFirst` workers of lock access and degrading overall tx verification throughput across the node. The `OnlySmallCycleTx` worker — whose purpose is to ensure small-cycle txs are processed promptly — is entirely consumed by scanning, defeating its design intent and causing small-cycle tx starvation as a secondary effect.

## Likelihood Explanation

Reachable by any unprivileged P2P peer on mainnet. The attacker only needs to relay structurally valid transactions with a declared cycle count above `max_tx_verify_cycles`. No PoW, no key material, no privileged access is required. The default configuration (`max_tx_verify_workers > 1` on any multi-core node) activates the vulnerable code path on all production nodes. The attack is repeatable and cheap: the attacker continuously replenishes the queue as `SubmitTimeFirst` workers drain it.

## Recommendation

1. **Fix the O(N) scan**: In `peek(only_small_cycle=true)`, use the existing `hashed_non_unique` index on `is_large_cycle` to perform an O(1) check for the existence of small-cycle entries before iterating. If no small-cycle entry exists, return `None` immediately without scanning.
2. **Fix the busy-wait**: When `OnlySmallCycleTx` finds no small-cycle tx, it should not call `re_notify()` unconditionally. It should only re-notify if there is at least one small-cycle tx in the queue (checkable in O(1) with the hash index). Otherwise it should sleep until a new tx is added.
3. **Rate-limit declared-large-cycle tx admission** per peer to limit queue flooding cost.

## Proof of Concept

```
1. Connect to a CKB node with max_tx_verify_workers >= 2 (default on any multi-core machine).
2. Craft N structurally valid minimum-size CKB transactions (no valid scripts needed).
3. Relay each tx via P2P with declared_cycles = max_tx_verify_cycles + 1.
4. All N txs enter the verify queue with is_large_cycle = true.
5. SubmitTimeFirst workers begin processing them inside their process_inner() loop.
6. OnlySmallCycleTx worker wakes up, acquires write lock, calls peek(true) → O(N) scan → None.
7. Worker calls re_notify(), releases write lock, returns to run(), immediately re-wakes.
8. Measure: CPU time in peek() grows linearly with N; write lock hold time blocks SubmitTimeFirst workers.
9. Assert: scan cost is O(N) not O(1); actual tx throughput drops proportionally.
```