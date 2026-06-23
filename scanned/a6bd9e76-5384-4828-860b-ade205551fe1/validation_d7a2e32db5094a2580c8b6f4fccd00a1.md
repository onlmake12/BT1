### Title
Write Lock Held Across Sequential `block_in_place` Script Execution in `readd_detached_tx` During Reorg — (`tx-pool/src/process.rs`, `tx-pool/src/util.rs`)

---

### Summary

`update_tx_pool_for_reorg` acquires the `tx_pool` async write lock and holds it for the entire duration of `readd_detached_tx`, which sequentially calls `verify_rtx` (via `block_in_place`) for every detached transaction without releasing the lock between verifications. An attacker with mining capability can craft a reorg whose detached blocks contain transactions with scripts consuming near-`max_tx_verify_cycles` cycles, causing the write lock to be held for N × M × (cycles/sec) seconds, freezing all concurrent tx-pool operations.

---

### Finding Description

In `tx-pool/src/process.rs`, `update_tx_pool_for_reorg` acquires the tokio async write lock at line 836:

```rust
let mut tx_pool = self.tx_pool.write().await;   // write lock acquired
_update_tx_pool_for_reorg(&mut tx_pool, ...);
self.readd_detached_tx(&mut tx_pool, retain, fetched_cache).await;  // lock still held
// lock dropped here at end of block
``` [1](#0-0) 

The write lock guard `tx_pool` lives until the closing brace at line 851. `readd_detached_tx` is called with `&mut TxPool` (a reborrow of the guard's inner value), but the guard itself remains alive in the outer scope across the `.await`. Inside `readd_detached_tx`, every detached transaction is verified sequentially in a loop:

```rust
for tx in txs {
    ...
    if let Ok(verified) = verify_rtx(snapshot, Arc::clone(&rtx), tx_env, &verify_cache, max_cycles, None).await {
``` [2](#0-1) 

The `command_rx` argument is `None`. In `verify_rtx`, when `command_rx` is `None` and there is no cache hit, execution falls into the `block_in_place` branch:

```rust
} else {
    block_in_place(|| {
        ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
            .verify(max_tx_verify_cycles, false)
            ...
    })
}
``` [3](#0-2) 

`block_in_place` runs synchronous, CPU-bound CKB-VM script execution on the current thread. The tokio async write lock guard is still held by the current task throughout this call. The result is that the write lock is held for the sum of all `block_in_place` durations across every detached transaction — sequentially, without any yield or lock release between them.

The `max_cycles` used is `self.tx_pool_config.max_tx_verify_cycles`: [4](#0-3) 

---

### Impact Explanation

While the write lock is held, every other task on the same tokio runtime that attempts to acquire the `tx_pool` write lock (or even a read lock, since a write lock is exclusive) is suspended. This includes:

- `submit_entry` (called by `_process_tx` for every incoming transaction)
- `with_tx_pool_write_lock` / `with_tx_pool_read_lock` (used by `get_block_template`, `get_tx_pool_info`, etc.)

The lock hold duration is proportional to **N_blocks × M_txs_per_block × max_tx_verify_cycles / (VM throughput)**. With `max_tx_verify_cycles` at the default of ~3.5 × 10⁹ cycles and VM throughput of ~10⁹ cycles/sec, a single max-cycle tx takes ~3.5 seconds. A reorg of 10 blocks × 10 txs = 100 transactions yields ~350 seconds of complete tx-pool admission freeze. During this window, no new transaction can be admitted, no block template can be assembled, and no tx-pool query can complete.

---

### Likelihood Explanation

The attacker needs valid PoW for N detached blocks. This does not require majority hashpower — a short reorg (1–3 blocks) is achievable with a small fraction of network hashpower through probabilistic mining on a private fork. The transactions themselves are valid (they were previously committed in the detached blocks), so no consensus rule is violated. The attack is repeatable: after the freeze ends, the attacker can trigger another reorg. The entry point is the standard P2P block relay path (`chain/src/verify.rs` → `tx_pool_controller.update_tx_pool_for_reorg`). [5](#0-4) 

---

### Recommendation

Release the write lock before script execution. The correct pattern — already used in `_process_tx` — is to perform `verify_rtx` outside the write lock and only re-acquire it for the brief `_submit_entry` call:

1. In `update_tx_pool_for_reorg`, drop the write lock after `_update_tx_pool_for_reorg` completes.
2. In `readd_detached_tx`, perform `resolve_tx` and `check_tx_fee` under a short-lived read lock (or snapshot), then call `verify_rtx` without any lock held, then re-acquire the write lock only for `_submit_entry`.

Alternatively, pass a `command_rx` to `verify_rtx` in `readd_detached_tx` so it uses the async `verify_with_pause` path instead of `block_in_place`, and restructure the lock acquisition to wrap only the pool mutation steps.

---

### Proof of Concept

1. Construct a private chain forking from block H with 10 blocks, each containing 10 transactions. Each transaction's lock script is a CKB-VM program that loops until it consumes `max_tx_verify_cycles` cycles (e.g., a tight loop with a cycle counter).
2. Mine valid PoW for all 10 blocks privately (requires outpacing the public chain for 10 blocks, feasible with ~30–50% hashpower or with a lucky streak at lower hashpower).
3. Broadcast the private chain to a target node via P2P block relay.
4. The node accepts the reorg (higher total difficulty), calls `update_tx_pool_for_reorg`, which acquires the write lock and enters `readd_detached_tx`.
5. Concurrently submit transactions to the node's RPC (`send_transaction`). Measure the response latency.
6. Assert: all concurrent submissions are blocked for approximately 10 × 10 × (max_tx_verify_cycles / VM_throughput) seconds, confirming the write lock is held throughout.

### Citations

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

**File:** tx-pool/src/process.rs (L884-903)
```rust
        let max_cycles = self.tx_pool_config.max_tx_verify_cycles;
        for tx in txs {
            let tx_size = tx.data().serialized_size_in_block();
            let tx_hash = tx.hash();
            if let Ok((rtx, status)) = resolve_tx(tx_pool, tx_pool.snapshot(), tx, false)
                && let Ok(fee) = check_tx_fee(tx_pool, tx_pool.snapshot(), &rtx, tx_size)
            {
                let verify_cache = fetched_cache.get(&tx_hash).cloned();
                let snapshot = tx_pool.cloned_snapshot();
                let tip_header = snapshot.tip_header();
                let tx_env = Arc::new(status.with_env(tip_header));
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

**File:** chain/src/verify.rs (L386-394)
```rust
            if tx_pool_controller.service_started() {
                if let Err(e) = tx_pool_controller.update_tx_pool_for_reorg(
                    fork.detached_blocks().clone(),
                    fork.attached_blocks().clone(),
                    fork.detached_proposal_id().clone(),
                    new_snapshot,
                ) {
                    error!("[verify block] notify update_tx_pool_for_reorg error {}", e);
                }
```
