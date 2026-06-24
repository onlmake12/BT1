Audit Report

## Title
TOCTOU Race in `_process_tx` Allows Concurrent Duplicate Script Verification — (File: `tx-pool/src/process.rs`)

## Summary
In `TxPoolService::_process_tx`, the read lock acquired during `pre_check` is released before the expensive `verify_rtx` script-execution step. Because the service loop spawns an independent Tokio task per received message, two concurrent `SubmitLocalTx` submissions of the same transaction can both pass all duplicate guards and both execute full RISC-V script verification before either commits to the pool. The second insertion is rejected as a duplicate, but both tasks have already completed full script verification, wasting CPU proportional to the number of concurrent submissions.

## Finding Description

**Service spawns one task per message** — confirmed at `service.rs:619-621`: [1](#0-0) 

This means N concurrent `SubmitLocalTx` messages run fully in parallel.

**`process_tx` (local path) checks for duplicates under short-lived read locks** — confirmed at `process.rs:409-411`: [2](#0-1) 

Both `verify_queue_contains` and `orphan_contains` acquire and immediately release their respective `RwLock` read guards. After these checks pass, `_process_tx` is called with no lock held.

**Inside `_process_tx`, the execution order is:**

1. `pre_check` at line 715 — acquires a `RwLock` read guard via `with_tx_pool_read_lock`, calls `check_txid_collision`, then **drops the lock** when the closure returns: [3](#0-2) [4](#0-3) 

2. `verify_rtx` at lines 724–732 — runs expensive RISC-V script execution via `block_in_place` with **no lock held**: [5](#0-4) [6](#0-5) 

3. `submit_entry` at line 753 — acquires a write lock and inserts the entry; this is the **first write** that prevents a duplicate: [7](#0-6) 

Because the read lock from `pre_check` is released before `verify_rtx` begins, a second concurrent task calling `pre_check` on the same transaction will also see the pool as empty and proceed to run `verify_rtx`. Both tasks then race to `submit_entry`; the second is rejected as a duplicate, but both have already completed full script verification.

**The verify cache does not mitigate this** — `fetch_tx_verify_cache` at line 719 checks by `witness_hash`. For the first batch of concurrent submissions, the cache is empty for all tasks. The cache is only populated asynchronously after the first task's `submit_entry` succeeds (lines 758–764), which is after `verify_rtx` has already been called by all concurrent tasks: [8](#0-7) 

**Contrast with the remote path** — `resumeble_process_tx` calls `enqueue_verify_queue`, which inserts into the verify queue under a write lock before verification begins, so a second concurrent call sees the tx as already queued and returns `Duplicated` immediately: [9](#0-8) 

The local `process_tx` path has no equivalent guard.

## Impact Explanation

`verify_rtx` executes up to `max_block_cycles` RISC-V cycles (~3.5 billion on mainnet). The `SubmitLocalTx` channel capacity is 512: [10](#0-9) 

With 512 concurrent submissions of the same maximum-cycle transaction, all 512 tasks pass `pre_check` (pool is empty), all 512 invoke `verify_rtx` concurrently via `block_in_place` (each blocking a Tokio worker thread), and the node's CPU is saturated for the duration of script execution. During this window the node is unresponsive to block relay, sync, and mining template generation. This maps to **High: Vulnerabilities which could easily crash a CKB node** (sustained unresponsiveness is operationally equivalent to a crash for block propagation purposes).

## Likelihood Explanation

The `send_transaction` RPC endpoint is the standard interface for wallets and SDKs. Any unprivileged local RPC caller can issue concurrent HTTP requests with no special keys or network position. The race window is wide — `verify_rtx` for a maximum-cycle transaction can take hundreds of milliseconds — making concurrent exploitation straightforward with a simple shell script. The attack is repeatable and requires no victim interaction.

## Recommendation

Apply the check-effects-interactions pattern before releasing the read lock:

1. **Add an "in-flight" set** — a `HashSet<Byte32>` (or reuse the verify queue) protected by a write lock. Before calling `verify_rtx`, insert the tx hash into this set under the same lock used by `pre_check`. Subsequent concurrent calls to `check_txid_collision` or a new `check_inflight` guard will see the tx as pending and return `Reject::Duplicated` immediately.
2. **Remove the in-flight marker** on completion (success or failure) of `verify_rtx` + `submit_entry`.
3. Alternatively, route all local submissions through `resumeble_process_tx` → `enqueue_verify_queue`, which already provides this guard via the verify queue write lock.

## Proof of Concept

```bash
# Submit a max-cycle transaction 512 times concurrently via local RPC
for i in $(seq 1 512); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<MAX_CYCLE_TX_HEX>,"passthrough"],"id":'"$i"'}' &
done
wait
```

All 512 tasks pass `pre_check` (pool is empty at the time each reads), all 512 invoke `verify_rtx` concurrently, node CPU pegs at 100% for the duration of script execution. Only one insertion succeeds; 511 verifications are wasted. The node becomes unresponsive to block relay and sync during this window.

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

**File:** tx-pool/src/process.rs (L276-314)
```rust
        let (ret, snapshot) = self
            .with_tx_pool_read_lock(|tx_pool, snapshot| {
                let tip_hash = snapshot.tip_hash();

                // Same txid means exactly the same transaction, including inputs, outputs, witnesses, etc.
                // It's also not possible for RBF, reject it directly
                check_txid_collision(tx_pool, tx)?;

                // Try normal path first, if double-spending check success we don't need RBF check
                // this make sure RBF won't introduce extra performance cost for hot path
                let res = resolve_tx(tx_pool, &snapshot, tx.clone(), false);
                match res {
                    Ok((rtx, status)) => {
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(Reject::Resolve(OutPointError::Dead(out))) => {
                        let (rtx, status) = resolve_tx(tx_pool, &snapshot, tx.clone(), true)?;
                        let fee = check_tx_fee(tx_pool, &snapshot, &rtx, tx_size)?;
                        let conflicts = tx_pool.pool_map.find_conflict_outpoint(tx);
                        if conflicts.is_none() {
                            // this mean one input's outpoint is dead, but there is no direct conflicted tx in tx_pool
                            // we should reject it directly and don't need to put it into conflicts pool
                            error!(
                                "{} is resolved as Dead, but there is no conflicted tx",
                                rtx.transaction.proposal_short_id()
                            );
                            return Err(Reject::Resolve(OutPointError::Dead(out)));
                        }
                        // we also return Ok here, so that the entry will be continue to be verified before submit
                        // we only want to put it into conflicts pool after the verification stage passed
                        // then we will double-check conflicts txs in `submit_entry`

                        Ok((tip_hash, rtx, status, fee, tx_size))
                    }
                    Err(err) => Err(err),
                }
            })
            .await;
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
