Audit Report

## Title
TOCTOU Race in `_process_tx` Allows Concurrent Duplicate Script Verification — (File: `tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the read lock acquired during `pre_check` is released before the expensive `verify_rtx` script-execution step. Because the service loop spawns an independent Tokio task per received `SubmitLocalTx` message, concurrent submissions of the same transaction can each pass `check_txid_collision` and proceed to full RISC-V script verification before any of them commits to the pool. Only the first task to reach `submit_entry` succeeds; all others waste CPU on redundant verification. The correct impact is **Low (501–2000 points): important performance improvement for CKB**.

## Finding Description

**Service spawns one task per message** — `service.rs:619-621` confirms each `SubmitLocalTx` message is dispatched as an independent Tokio task:
```rust
Some(message) = receiver.recv() => {
    let service_clone = process_service.clone();
    handle_clone.spawn(process(service_clone, message));
```
`Message::SubmitLocalTx` is handled at `service.rs:805-812` by calling `service.process_tx(tx, None).await`.

**`process_tx` duplicate guards are non-atomic** — `process.rs:409-411` checks `verify_queue_contains` and `orphan_contains` under separate, immediately-released read locks. After both pass, `_process_tx` is called with no lock held.

**Inside `_process_tx`, the execution order is:**

1. `pre_check` at `process.rs:715` — calls `with_tx_pool_read_lock` (`process.rs:247-256`), which acquires a `RwLock` read guard, runs `check_txid_collision` (`util.rs:20-26`), then **drops the lock** when the closure returns.
2. `verify_rtx` at `process.rs:724-732` — runs expensive RISC-V script execution via `block_in_place` (`util.rs:117-130`) with **no lock held**.
3. `submit_entry` at `process.rs:753` — acquires a write lock (`process.rs:102-103`) and inserts the entry; this is the **first write** that prevents a duplicate.

Because the read lock from `pre_check` is released before `verify_rtx` begins, a second concurrent task calling `pre_check` on the same transaction will also see the pool as empty and proceed to run `verify_rtx`. Both tasks then race to `submit_entry`; the second is rejected as a duplicate, but both have already completed full script verification.

**The verify cache does not mitigate this** — `fetch_tx_verify_cache` at `process.rs:719` checks by `witness_hash`. The cache is only populated asynchronously after the first task's `submit_entry` succeeds (`process.rs:758-764`), which is after `verify_rtx` has already been called by all concurrent tasks.

**Contrast with the remote path** — `resumeble_process_tx` (`process.rs:335-352`) calls `enqueue_verify_queue` (`process.rs:860-868`), which acquires a write lock on the verify queue and calls `add_tx` (`verify_queue.rs:198-236`). The `add_tx` method checks for duplicates under the write lock (`verify_queue.rs:204-209`), so a second concurrent call sees the tx as already queued and returns immediately without proceeding to verification. The local `process_tx` path has no equivalent atomic guard.

## Impact Explanation

**Low (501–2000 points): Any other important performance improvements for CKB.**

The race causes redundant CPU work: multiple concurrent tasks each run full RISC-V script verification (up to `max_block_cycles` ≈ 3.5 billion cycles per task) for the same transaction, with only one insertion succeeding. This wastes CPU proportional to the number of concurrent submissions. The node does not crash and recovers after verification completes. The attack requires local RPC access (port 8114, typically bound to localhost), so it is not reachable by an unprivileged remote attacker. The claimed "High: easily crash a CKB node" severity is not supported — the node process does not terminate, and the async runtime continues to handle other tasks (block relay, sync) albeit with degraded throughput during the verification window.

## Likelihood Explanation

Any process with local access to the RPC port can issue concurrent `send_transaction` HTTP requests. The race window is wide — `verify_rtx` for a maximum-cycle transaction can take hundreds of milliseconds — making concurrent exploitation straightforward. However, the attack surface is limited to local callers (wallets, SDKs, or other local processes), not arbitrary remote peers.

## Recommendation

Apply the check-then-reserve pattern before releasing the read lock:

1. **Add an "in-flight" set** — a `HashSet<Byte32>` (or reuse the verify queue) protected by a write lock. Before calling `verify_rtx`, insert the tx hash into this set under the same lock used by `pre_check`. Subsequent concurrent calls to `check_txid_collision` or a new `check_inflight` guard will see the tx as pending and return `Reject::Duplicated` immediately.
2. **Remove the in-flight marker** on completion (success or failure) of `verify_rtx` + `submit_entry`.
3. Alternatively, route all local submissions through `resumeble_process_tx` → `enqueue_verify_queue`, which already provides this guard via the verify queue write lock and `add_tx`'s duplicate check.

## Proof of Concept

```bash
# Submit a max-cycle transaction N times concurrently via local RPC
for i in $(seq 1 64); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<MAX_CYCLE_TX_HEX>,"passthrough"],"id":'"$i"'}' &
done
wait
```

All N tasks pass `pre_check` (pool is empty at the time each reads), all N invoke `verify_rtx` concurrently. Only one insertion succeeds; N-1 verifications are wasted. CPU utilization spikes for the duration of script execution. The effect is repeatable and observable via node metrics (`ckb_tx_pool_sync_process` histogram) and CPU profiling.