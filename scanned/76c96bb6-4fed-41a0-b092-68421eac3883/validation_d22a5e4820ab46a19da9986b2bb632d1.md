### Title
Silently Discarded Return Value of `add_tx` in RBF Recovery Path Causes Unnotified Transaction Loss — (File: `tx-pool/src/process.rs`)

---

### Summary

Inside `TxPoolService::submit_entry`, after an RBF replacement removes conflicting transactions, recovered transactions are re-queued via `queue.add_tx(tx, false, None)` inside a `tokio::spawn` closure. The `Result<bool, Reject>` return value is unconditionally discarded with `let _ =`. If re-queuing fails for any reason (e.g., queue full), the failure is silent: no error is logged, no reject callback is fired, and the transaction is permanently lost from the pool without any notification to its submitter.

---

### Finding Description

In `submit_entry`, after `process_rbf` removes conflicting transactions and identifies recoverable ones, the following code runs:

```rust
if !may_recovered_txs.is_empty() {
    let self_clone = self.clone();
    tokio::spawn(async move {
        let mut queue = self_clone.verify_queue.write().await;
        for tx in may_recovered_txs {
            debug!("recover back: {:?}", tx.proposal_short_id());
            let _ = queue.add_tx(tx, false, None);   // ← return value silently discarded
        }
    });
}
``` [1](#0-0) 

`add_tx` returns `Result<bool, Reject>`. It can fail with `Reject::Full` when the verify queue is at capacity, or with other `Reject` variants. By using `let _ =`, every failure path is silently swallowed. No `warn!`/`error!` is emitted, no reject callback (`self.callbacks.call_reject`) is invoked, and the transaction is permanently removed from the pool with no trace.

The `process_rbf` method that produces `may_recovered_txs` is called unconditionally whenever RBF conflicts exist: [2](#0-1) 

The recovered transactions are those whose inputs become available again after the conflicting transactions are evicted: [3](#0-2) 

---

### Impact Explanation

Transactions that were legitimately in the pool and displaced by an RBF replacement can be permanently and silently lost. The original submitters receive no notification — no reject callback, no RPC error, no log entry. This breaks the expected RBF semantic: displaced transactions should either be re-admitted or explicitly rejected. Silent loss means the tx-pool state diverges from what submitters expect, and transactions representing value transfers are dropped without any trace or recourse.

---

### Likelihood Explanation

An unprivileged tx-pool submitter can trigger this path with only standard RPC access:

1. Flood the verify queue with many low-fee transactions to fill it to its bounded capacity (`BLOCK_DOWNLOAD_WINDOW * 10` entries).
2. Submit a high-fee transaction that conflicts with (and RBF-replaces) existing pool transactions whose inputs are shared with other pool transactions.
3. `process_rbf` removes the conflicting transactions and identifies recoverable ones.
4. The `tokio::spawn` closure attempts to re-add the recovered transactions via `add_tx`.
5. Since the queue is full, `add_tx` returns `Reject::Full`.
6. The error is silently discarded; the recovered transactions are permanently lost.

The verify queue is bounded: [4](#0-3) 

RBF is a standard, documented feature reachable by any tx-pool submitter via the `send_transaction` RPC.

---

### Recommendation

Replace the silent discard with explicit error handling:

```rust
for tx in may_recovered_txs {
    debug!("recover back: {:?}", tx.proposal_short_id());
    if let Err(reject) = queue.add_tx(tx.clone(), false, None) {
        warn!(
            "Failed to recover tx {} after RBF replacement: {}",
            tx.proposal_short_id(),
            reject
        );
        // Optionally invoke self_clone.callbacks.call_reject or record in recent_reject
    }
}
```

This ensures failures are observable and, if needed, propagated to subscribers or logged for diagnosis.

---

### Proof of Concept

1. Connect to a CKB node with RPC access.
2. Submit enough transactions via `send_transaction` to fill the verify queue to its capacity (`BLOCK_DOWNLOAD_WINDOW * 10`).
3. Submit a new transaction with a higher fee rate that conflicts with (spends the same inputs as) an existing pool transaction, satisfying the RBF rules.
4. `submit_entry` → `process_rbf` removes the conflicting transaction and populates `may_recovered_txs`.
5. The spawned task calls `queue.add_tx(tx, false, None)` on a full queue.
6. `add_tx` returns `Err(Reject::Full)`.
7. `let _ =` discards the error; the recovered transaction is permanently gone from the pool.
8. No log entry, no callback, no RPC error is produced for the lost transaction. [1](#0-0)

### Citations

**File:** tx-pool/src/process.rs (L136-136)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
```

**File:** tx-pool/src/process.rs (L154-164)
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
                }
```

**File:** tx-pool/src/process.rs (L218-218)
```rust
        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
```

**File:** chain/src/init.rs (L49-50)
```rust
    let (preload_unverified_tx, preload_unverified_rx) =
        channel::bounded::<LonelyBlockHash>(BLOCK_DOWNLOAD_WINDOW as usize * 10);
```
