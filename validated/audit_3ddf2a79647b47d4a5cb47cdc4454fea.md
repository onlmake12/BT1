Audit Report

## Title
TOCTOU Race in `_process_tx` Allows Redundant Concurrent Script Verification — (File: `tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the read lock acquired during `pre_check` is released before the expensive `verify_rtx` script-execution step. Because the service loop spawns an independent Tokio task per received message, two concurrent `SubmitLocalTx` submissions of the same transaction can both pass all duplicate guards and both execute full RISC-V script verification before either commits to the pool. The second insertion is rejected as a duplicate, but both tasks have already completed full script verification, wasting CPU. This is triggerable via the local `send_transaction` RPC endpoint.

## Finding Description

**Service spawns one task per message** — confirmed at `service.rs:619-621`: [1](#0-0) 

Each received message spawns an independent Tokio task, so N concurrent `SubmitLocalTx` messages run fully in parallel.

**`process_tx` (local path) checks for duplicates under short-lived, non-atomic locks** — at `process.rs:409-411`: [2](#0-1) 

`verify_queue_contains` acquires and immediately releases a read guard on `self.verify_queue`; `orphan_contains` does the same on `self.orphan`. After both checks pass, `_process_tx` is called with no lock held.

**Inside `_process_tx`, the execution order is:**

1. `pre_check` at line 715 — calls `with_tx_pool_read_lock`, which acquires `self.tx_pool.read().await`, runs `check_txid_collision` and `resolve_tx`, then **drops the read lock** when the closure returns: [3](#0-2) [4](#0-3) 

2. `verify_rtx` at lines 724–732 — runs expensive RISC-V script execution via `block_in_place` with **no lock held**: [5](#0-4) [6](#0-5) 

3. `submit_entry` at line 753 — acquires a write lock and inserts the entry; this is the **first write** that prevents a duplicate: [7](#0-6) 

Because the read lock from `pre_check` is released before `verify_rtx` begins, a second concurrent task calling `pre_check` on the same transaction will also see the pool as empty and proceed to run `verify_rtx`. Both tasks then race to `submit_entry`; the second is rejected as a duplicate, but both have already completed full script verification.

**The verify cache does not mitigate this** — `fetch_tx_verify_cache` checks by `witness_hash`. For the first batch of concurrent submissions, the cache is empty for all tasks. The cache is only populated asynchronously after the first task's `submit_entry` succeeds (lines 758–764), which is after `verify_rtx` has already been called by all concurrent tasks: [8](#0-7) 

**Contrast with the remote path** — `resumeble_process_tx` calls `enqueue_verify_queue` which inserts into the verify queue under a write lock before verification begins, so a second concurrent call sees the tx as already queued and returns `Duplicated` immediately: [9](#0-8) 

The local `process_tx` path has no equivalent guard.

## Impact Explanation

This vulnerability maps to **Note (0–500 points): Any local RPC API crash**. The `send_transaction` RPC endpoint is bound to localhost by default. An attacker with local access can submit the same maximum-cycle transaction concurrently, causing all concurrent tasks to execute full RISC-V script verification (up to `max_block_cycles` cycles, ~3.5 billion on mainnet) via `block_in_place`, each blocking a Tokio worker thread. The channel capacity is 512: [10](#0-9) 

The node becomes temporarily unresponsive during the verification window. The claimed "High" severity is not supported: the node does not crash, the effect is temporary and bounded by verification time, and the attack requires local RPC access. The correct impact is the "Note" category for local RPC-triggered unresponsiveness.

## Likelihood Explanation

Requires local access to the RPC endpoint (default: `127.0.0.1:8114`). Any unprivileged local process can issue concurrent HTTP requests to `send_transaction` with no special keys. The race window is wide — `verify_rtx` for a maximum-cycle transaction can take hundreds of milliseconds — making concurrent exploitation straightforward. The attack is repeatable and can be sustained continuously to keep the node CPU-saturated.

## Recommendation

Apply the check-effects-interactions pattern before releasing the read lock:

1. **Add an "in-flight" set** — a `HashSet<Byte32>` protected by a write lock. Before calling `verify_rtx`, insert the tx hash into this set under the same lock used by `pre_check`. Subsequent concurrent calls to `check_txid_collision` or a new `check_inflight` guard will see the tx as pending and return `Reject::Duplicated` immediately.
2. **Remove the in-flight marker** on completion (success or failure) of `verify_rtx` + `submit_entry`.
3. Alternatively, route all local submissions through `resumeble_process_tx` → `enqueue_verify_queue`, which already provides this guard via the verify queue write lock.

## Proof of Concept

```bash
# Submit a max-cycle transaction concurrently via local RPC
for i in $(seq 1 512); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<MAX_CYCLE_TX_HEX>,"passthrough"],"id":'"$i"'}' &
done
wait
```

All concurrent tasks pass `pre_check` (pool is empty at the time each reads), all invoke `verify_rtx` concurrently via `block_in_place`, node CPU pegs at 100% for the duration of script execution. Only one insertion succeeds; the rest are wasted. The node becomes unresponsive to block relay and sync during this window.

### Citations

**File:** tx-pool/src/service.rs (L53-53)
```rust
pub(crate) const DEFAULT_CHANNEL_SIZE: usize = 512;
```

**File:** tx-pool/src/service.rs (L619-621)
```rust
                    Some(message) = receiver.recv() => {
                        let service_clone = process_service.clone();
                        handle_clone.spawn(process(service_clone, message));
```

**File:** tx-pool/src/process.rs (L247-256)
```rust
    pub(crate) async fn with_tx_pool_read_lock<U, F: FnMut(&TxPool, Arc<Snapshot>) -> U>(
        &self,
        mut f: F,
    ) -> (U, Arc<Snapshot>) {
        let tx_pool = self.tx_pool.read().await;
        let snapshot = tx_pool.cloned_snapshot();

        let ret = f(&tx_pool, Arc::clone(&snapshot));
        (ret, snapshot)
    }
```

**File:** tx-pool/src/process.rs (L349-352)
```rust
        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
```

**File:** tx-pool/src/process.rs (L409-411)
```rust
        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
```

**File:** tx-pool/src/process.rs (L715-717)
```rust
        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);
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

**File:** tx-pool/src/process.rs (L753-754)
```rust
        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
        try_or_return_with_snapshot!(ret, submit_snapshot);
```

**File:** tx-pool/src/process.rs (L758-764)
```rust
        if verify_cache.is_none() {
            // update cache
            let txs_verify_cache = Arc::clone(&self.txs_verify_cache);
            tokio::spawn(async move {
                let mut guard = txs_verify_cache.write().await;
                guard.put(wtx_hash, verified);
            });
```

**File:** tx-pool/src/util.rs (L117-130)
```rust
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
```
