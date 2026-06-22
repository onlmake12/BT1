### Title
Silently Discarded RBF-Recovered Transactions in Verify Queue Re-Admission — (File: `tx-pool/src/process.rs`)

---

### Summary
When an RBF (Replace-By-Fee) replacement succeeds, previously conflicting transactions that are no longer conflicting are supposed to be re-admitted to the verify queue. The result of `queue.add_tx()` for each recovered transaction is silently discarded with `let _ = ...`. If re-admission fails (e.g., queue is full), those transactions are permanently lost from the pool with no error propagation, no rejection record, and no notification to the original submitter.

---

### Finding Description

In `tx-pool/src/process.rs`, inside `submit_entry`, after a successful RBF replacement the code spawns a task to push recovered transactions back into the verify queue:

```rust
if !may_recovered_txs.is_empty() {
    let self_clone = self.clone();
    tokio::spawn(async move {
        let mut queue = self_clone.verify_queue.write().await;
        for tx in may_recovered_txs {
            debug!("recover back: {:?}", tx.proposal_short_id());
            let _ = queue.add_tx(tx, false, None);   // ← result silently dropped
        }
    });
}
``` [1](#0-0) 

`queue.add_tx()` returns `Result<bool, Reject>`. The `Reject` variant can be `Reject::Full(_)` when the verify queue has reached its capacity limit. By binding the return value to `let _ = ...`, any failure is unconditionally swallowed. There is no:
- fallback path (e.g., direct pool insertion or retry),
- call to `put_recent_reject` to record the loss,
- call to `send_result_to_relayer` to notify the network layer,
- or any log at `warn`/`error` level.

The recovered transactions are simply gone.

Compare this to the error-aware path used elsewhere in the same file when enqueuing orphan transactions:

```rust
match self.enqueue_verify_queue(orphan.tx.clone(), false, ...).await {
    Ok(_) => { self.remove_orphan_tx(&orphan_id).await; }
    Err(reject) => { warn!("process_orphan {} failed to enqueue verify queue: {}", ...); }
}
``` [2](#0-1) 

The orphan path handles the error; the RBF-recovery path does not.

---

### Impact Explanation

Recovered transactions are transactions that were legitimately in the pool, were evicted solely because they conflicted with the RBF replacer, and are now conflict-free. Silently dropping them means:

1. **Transactions permanently lost** — they are removed from `pool_map` by `remove_entry_and_descendants` during RBF processing and never re-enter any pool structure.
2. **No rejection record** — `recent_reject` is not updated, so `get_tx_status` returns `Unknown` rather than `Rejected`, misleading callers.
3. **No relay notification** — `send_result_to_relayer` is not called, so the network layer does not mark these transactions as unknown and will not re-request them from peers.
4. **Silent capacity-triggered loss** — a high-throughput period or a targeted flood of transactions can fill the verify queue, causing every subsequent RBF recovery to silently discard its recovered set.

The net effect is analogous to the reported vault bug: assets (here, valid pending transactions representing user funds) enter a processing path, a sub-operation fails without revert, and the assets are permanently stuck/lost with no observable error.

---

### Likelihood Explanation

The verify queue has a bounded capacity (`max_tx_verify_cycles`-based limit in `VerifyQueue`). Under normal mainnet load the queue may not be full, but:

- An unprivileged peer can flood the tx-pool with high-cycle transactions to saturate the verify queue.
- Once saturated, any RBF replacement submitted by any user triggers the silent-drop path for all recovered transactions.
- No special privilege is required; a standard `send_transaction` RPC call or a relayed transaction is sufficient.

---

### Recommendation

Replace the silent discard with explicit error handling, mirroring the orphan-processing pattern already present in the same file:

```rust
for tx in may_recovered_txs {
    debug!("recover back: {:?}", tx.proposal_short_id());
    match queue.add_tx(tx.clone(), false, None) {
        Ok(_) => {}
        Err(reject) => {
            warn!(
                "RBF recovery: failed to re-enqueue {}: {}",
                tx.hash(), reject
            );
            // Optionally: record in recent_reject and notify relayer
        }
    }
}
```

If the queue is full, the node should at minimum log a `warn`-level message and record the rejection so that `get_tx_status` returns an accurate result.

---

### Proof of Concept

1. Fill the verify queue to capacity by submitting many large-cycle transactions via RPC (`send_transaction`).
2. Submit a transaction T1 that spends output O.
3. Submit a higher-fee transaction T2 (RBF) that also spends output O, replacing T1.
4. T1 is removed from `pool_map` and placed in `may_recovered_txs` (it no longer conflicts after T2 takes O).
5. The spawned task calls `queue.add_tx(T1, false, None)` — the queue is full, `Reject::Full` is returned.
6. `let _ = ...` discards the error.
7. T1 is now absent from every pool structure. `get_tx_status(T1.hash())` returns `Unknown`. The original submitter of T1 receives no rejection notification and their transaction is permanently lost from the node's perspective. [1](#0-0) [3](#0-2)

### Citations

**File:** tx-pool/src/process.rs (L154-163)
```rust
                if !may_recovered_txs.is_empty() {
                    let self_clone = self.clone();
                    tokio::spawn(async move {
                        // push the recovered txs back to verify queue, so that they can be verified and submitted again
                        let mut queue = self_clone.verify_queue.write().await;
                        for tx in may_recovered_txs {
                            debug!("recover back: {:?}", tx.proposal_short_id());
                            let _ = queue.add_tx(tx, false, None);
                        }
                    });
```

**File:** tx-pool/src/process.rs (L203-234)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
```

**File:** tx-pool/src/process.rs (L605-624)
```rust
                    match self
                        .enqueue_verify_queue(
                            orphan.tx.clone(),
                            false,
                            Some((orphan.cycle, orphan.peer)),
                        )
                        .await
                    {
                        Ok(_) => {
                            self.remove_orphan_tx(&orphan_id).await;
                        }
                        Err(reject) => {
                            warn!(
                                "process_orphan {} failed to enqueue verify queue: {}; keep orphan from {}",
                                orphan.tx.hash(),
                                reject,
                                tx.hash(),
                            );
                        }
                    }
```
