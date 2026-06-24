Audit Report

## Title
Unbounded Inline BFS Orphan Processing in `process_orphan_tx` Stalls Verify Workers — (`tx-pool/src/process.rs`)

## Summary

`process_orphan_tx` in `tx-pool/src/process.rs` performs an unbounded BFS traversal over the orphan pool with no per-call iteration cap. An unprivileged remote peer can pre-fill the orphan pool with up to 100 chained orphan transactions, then submit the root transaction to trigger sequential inline script verification of all 100 orphans within a single verify-worker invocation, blocking that worker from processing any other queued transactions until the entire chain is drained.

## Finding Description

`process_orphan_tx` (L591–671) uses an unbounded `while let Some(previous) = orphan_queue.pop_front()` BFS loop with no iteration limit:

```rust
while let Some(previous) = orphan_queue.pop_front() {
    let orphans = self.find_orphan_by_previous(&previous).await;
    for orphan in orphans.into_iter() {
        if orphan.cycle > self.tx_pool_config.max_tx_verify_cycles {
            // re-enqueue to verify queue (async path)
        } else if let Some((ret, _snapshot)) = self
            ._process_tx(orphan.tx.clone(), Some(orphan.cycle), None)  // command_rx: None
            .await
        { ... orphan_queue.push_back(orphan.tx); }  // cascade
    }
}
```

For orphans whose declared cycle is ≤ `max_tx_verify_cycles`, `_process_tx` is called with `command_rx: None` (L626) — the non-pausable synchronous path. On success, the resolved orphan is pushed back into `orphan_queue` (L641), cascading through the entire chain. The orphan pool is capped at `DEFAULT_MAX_ORPHAN_TRANSACTIONS = 100` (orphan.rs L16), but the BFS loop has no corresponding per-call bound.

The verify worker's `process_inner` loop (verify_mgr.rs L147–158) calls `_process_tx` with `Some(&mut self.command_rx)` (pausable) for queue entries, then calls `after_process` → `process_orphan_tx` synchronously before returning to pick the next entry from the verify queue. The worker is therefore occupied for the entire duration of the orphan chain traversal.

## Impact Explanation

A single verify worker is blocked from processing any other `VerifyQueue` entries for the duration of the unbounded orphan cascade — up to 100 inline `_process_tx` calls, each performing full `verify_rtx` script execution with no pause/suspend mechanism. With scripts constructed to consume close to `max_tx_verify_cycles` (70,000,000 cycles, resource/ckb.toml L215), the stall duration is significant. The default worker count is `max(num_cpus * 3/4, 1)` (tx_pool.rs L47); an attacker can repeat the attack to stall all workers simultaneously. This constitutes **CKB network congestion with few costs** — a High-severity impact.

## Likelihood Explanation

The attack requires only an unprivileged P2P peer with a small amount of CKB (to fund the root transaction's live-cell input). Constructing 100 chained orphan transactions is low-cost. The orphan pool's eviction policy (random eviction when full) does not prevent a linear chain from being retained if submitted in reverse order. The attack is repeatable: after the orphan pool drains, the attacker can immediately refill it and trigger another cascade.

## Recommendation

Introduce a per-call iteration limit to `process_orphan_tx`. Process at most `N` orphans per invocation (e.g., `N = 10`) and re-schedule remaining work asynchronously via `enqueue_verify_queue` or a deferred task. Orphans with declared cycle ≤ `max_tx_verify_cycles` should also be re-enqueued to the verify queue (as is already done for the `cycle > max_tx_verify_cycles` branch at L598–624) rather than processed inline, so the verify worker's pause/resume mechanism (`command_rx`) remains active and the worker can interleave other queued transactions.

## Proof of Concept

1. Connect to a CKB node as a P2P peer. Own one live cell (UTXO) as `cell[0]`.
2. Construct a linear chain of 100 transactions: `tx[0]` spends `cell[0]`; `tx[i]` spends output 0 of `tx[i-1]` for `i = 1..99`. Each `tx[i]` for `i ≥ 1` carries a lock script that consumes ~70,000,000 cycles. Declare `cycle = max_tx_verify_cycles` for each.
3. Submit `tx[1]` through `tx[99]` via P2P relay. Each is added to the orphan pool (input missing). The pool fills to 100 entries (orphan.rs L119–125 evicts only when `len > 100`).
4. Submit `tx[0]`. It passes pre-check and enters the verify queue normally.
5. The verify worker picks up `tx[0]`, verifies it (pausable, with `command_rx`), calls `after_process` → `process_orphan_tx(&tx[0])`.
6. `process_orphan_tx` enters the BFS loop: finds `tx[1]`, calls `_process_tx(tx[1], Some(70M_cycles), None)` (non-pausable), succeeds, pushes `tx[1]` into `orphan_queue`; finds `tx[2]`, calls `_process_tx(tx[2], ...)`, and so on through all 100 orphans — all within a single invocation, with no iteration cap (process.rs L595–670).
7. The verify worker is blocked for the entire duration. All other transactions in the `VerifyQueue` are delayed until the loop completes. Repeat from step 2 to stall additional workers.