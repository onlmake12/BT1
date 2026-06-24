The code confirms all claims in the report. Let me verify the key facts:

**L836 — write lock acquired, held across `readd_detached_tx`:** [1](#0-0) 

**L895–903 — `verify_rtx` called with `command_rx = None` inside the lock:** [2](#0-1) 

**`util.rs` L116–131 — `command_rx = None` branch executes `block_in_place`:** [3](#0-2) 

**`_process_tx` L724–753 — correct pattern: `verify_rtx` called before any write lock:** [4](#0-3) 

All four code claims are confirmed exactly as described. The vulnerability is real.

---

Audit Report

## Title
Tx-Pool Write Lock Held Across `block_in_place` CKB-VM Execution in `readd_detached_tx` During Reorg — (File: tx-pool/src/process.rs)

## Summary
In `update_tx_pool_for_reorg`, the `tx_pool` write lock is acquired at L836 and held for the entire duration of `readd_detached_tx` (L849–850), which calls `verify_rtx` with `command_rx = None` for each cache-missing detached transaction. This routes to `block_in_place` in `util.rs` (L117), executing full CKB-VM script verification synchronously while the write lock remains held. All concurrent tx-pool write operations — including new transaction submission and block template generation — are blocked for the full duration of script execution per detached transaction.

## Finding Description
In `update_tx_pool_for_reorg` (`process.rs` L836–851), the write lock is acquired at L836 and held across the entire `readd_detached_tx` call:

```rust
let mut tx_pool = self.tx_pool.write().await;   // L836: write lock acquired
_update_tx_pool_for_reorg(...);
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;  // L849: lock still held
```

Inside `readd_detached_tx` (L878–914), for each transaction, `verify_rtx` is called with `command_rx = None` (L901):

```rust
if let Ok(verified) = verify_rtx(snapshot, Arc::clone(&rtx), tx_env, &verify_cache, max_cycles, None).await
```

In `verify_rtx` (`util.rs` L116–131), the `command_rx = None` branch executes `block_in_place`:

```rust
} else {
    block_in_place(|| {
        ContextualTransactionVerifier::new(...).verify(max_tx_verify_cycles, false)...
    })
}
```

`block_in_place` moves the current thread to a blocking pool, but the `tokio::sync::RwLock` write guard remains owned by the current async task. Any other async task calling `self.tx_pool.write().await` queues behind this guard for the entire CKB-VM execution duration.

The `fetched_cache` is populated at L828 before the lock, but only contains entries for transactions previously processed through `_process_tx` on this node. Transactions mined without passing through the victim node's mempool (e.g., submitted directly to a miner's RPC) will always be cache misses, forcing the full `block_in_place` path. Cache updates are spawned async tasks (L761–764) that may not have committed before a reorg, and LRU eviction can also clear entries.

By contrast, `_process_tx` (L705–777) correctly separates concerns: `verify_rtx` is called at L724–732 before any write lock is acquired, and `submit_entry` (which acquires the write lock) is called only after verification completes at L753. `readd_detached_tx` does not follow this pattern.

## Impact Explanation
While the write lock is held during `block_in_place` execution, `submit_entry` for new transactions is blocked, and critically, block template generation (`update_full`) is blocked — mining nodes cannot produce new block templates. For N detached transactions each running up to `max_tx_verify_cycles` cycles (default 70,000,000), the total lock hold time is O(N × max script execution time). An attacker sustaining repeated 1-block reorgs causes persistent block template stalls on targeted mining nodes, contributing to CKB network congestion. This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**.

## Likelihood Explanation
A 1-block reorg requires no majority hashpower — it occurs naturally when two miners find blocks at the same height. An attacker with any mining capacity can deliberately produce a competing block at the same height. The attacker does not need to control the victim node or have any special privileges. The heavy-script transaction is submitted directly to the miner's RPC (bypassing the victim node's mempool) to guarantee a cache miss. The attack is repeatable: each new competing block at the same height triggers another reorg cycle.

## Recommendation
Move script verification outside the write lock scope, mirroring the pattern already used in `_process_tx`. In `update_tx_pool_for_reorg`, resolve and verify all detached transactions before acquiring the write lock, then acquire the lock only for pool mutation via `_submit_entry`:

```rust
// Phase 1: resolve and verify outside the lock (no lock held)
let mut verified_entries = Vec::new();
for tx in retain {
    if let Ok((rtx, status)) = resolve_tx_readonly(&snapshot, tx) {
        if let Ok(verified) = verify_rtx(snapshot, rtx, tx_env, &verify_cache, max_cycles, None).await {
            verified_entries.push((rtx, status, verified, fee, tx_size));
        }
    }
}

// Phase 2: acquire write lock only for pool mutation
{
    let mut tx_pool = self.tx_pool.write().await;
    _update_tx_pool_for_reorg(&mut tx_pool, ...);
    for (rtx, status, verified, fee, tx_size) in verified_entries {
        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);
        let _ = _submit_entry(&mut tx_pool, status, entry, &self.callbacks);
    }
}
```

Note that `resolve_tx` currently takes `&mut TxPool`, so a read-only resolution path may need to be introduced, or resolution can be deferred to inside the lock while keeping verification outside.

## Proof of Concept
1. Craft `TX_heavy`: a transaction whose lock script loops for `max_tx_verify_cycles` cycles.
2. Submit `TX_heavy` directly to Miner M's RPC (not through the victim node), ensuring the victim's `txs_verify_cache` has no entry for it.
3. Miner M mines `Block_N` containing `TX_heavy`. Victim node receives and stores `Block_N`.
4. Attacker mines `Block_N'` at the same height (any hashpower, or wait for a natural fork).
5. Victim node receives `Block_N'`, triggering a reorg: `update_tx_pool_for_reorg` is called with `retain = [TX_heavy]`, `fetched_cache = {}`.
6. At `process.rs` L836, write lock is acquired. `readd_detached_tx` is called. Cache miss → `verify_rtx(..., None)` → `block_in_place` runs full CKB-VM for `TX_heavy`. Write lock held for entire execution.
7. During step 6, any concurrent call to `self.tx_pool.write().await` (new tx submission, block template update, shutdown) blocks until CKB-VM finishes.
8. Attacker repeats step 4 to sustain the DoS.

### Citations

**File:** tx-pool/src/process.rs (L724-753)
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

        let verified = try_or_return_with_snapshot!(verified_ret, snapshot);

        if let Some(declared) = declared_cycles
            && declared != verified.cycles
        {
            info!(
                "process_tx declared cycles not match verified cycles, declared: {}, verified: {}, tx_hash: {}",
                declared,
                verified.cycles,
                tx.hash()
            );
            return Some((
                Err(Reject::DeclaredWrongCycles(declared, verified.cycles)),
                snapshot,
            ));
        }

        let entry = TxEntry::new(rtx, verified.cycles, fee, tx_size);

        let (ret, submit_snapshot) = self.submit_entry(tip_hash, entry, status).await;
```

**File:** tx-pool/src/process.rs (L836-851)
```rust
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }
```

**File:** tx-pool/src/process.rs (L895-903)
```rust
                if let Ok(verified) = verify_rtx(
                    snapshot,
                    Arc::clone(&rtx),
                    tx_env,
                    &verify_cache,
                    max_cycles,
                    None,
                )
                .await
```

**File:** tx-pool/src/util.rs (L116-131)
```rust
    } else {
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
    }
```
