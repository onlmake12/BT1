### Title
Unchecked Return Value of `queue.add_tx()` Silently Drops RBF-Recovered Transactions — (`tx-pool/src/process.rs`)

---

### Summary

Inside `TxPoolService::submit_entry`, when RBF (Replace-By-Fee) replacement succeeds, displaced transactions that may still be valid are pushed back into the verify queue via `queue.add_tx()`. The `Result<bool, Reject>` returned by `add_tx` is unconditionally discarded with `let _ =`. If the call fails for any reason (e.g., queue full, duplicate detection, or any other rejection), the failure is silently swallowed and the recovered transactions are permanently lost from the mempool with no error, no log, and no notification to the submitter.

---

### Finding Description

In `tx-pool/src/process.rs`, the `submit_entry` method handles RBF conflict resolution. After removing conflicting transactions, it collects a set of `may_recovered_txs` — transactions whose inputs are no longer consumed by the new RBF transaction and which could be re-verified and re-submitted. These are pushed back into the verify queue inside a `tokio::spawn` closure:

```rust
if !may_recovered_txs.is_empty() {
    let self_clone = self.clone();
    tokio::spawn(async move {
        let mut queue = self_clone.verify_queue.write().await;
        for tx in may_recovered_txs {
            debug!("recover back: {:?}", tx.proposal_short_id());
            let _ = queue.add_tx(tx, false, None);  // ← return value discarded
        }
    });
}
```

`queue.add_tx()` returns `Result<bool, Reject>`. The `Reject` enum includes variants such as `Reject::Full` (queue capacity exceeded), `Reject::Duplicated`, and others. By using `let _ =`, all failure cases are silently ignored. There is no fallback, no logging of the failure reason, and no mechanism to surface the loss to the caller or the original transaction submitter. [1](#0-0) 

---

### Impact Explanation

When `add_tx` fails silently:

1. **Permanent mempool loss**: Transactions that were valid and should have been re-queued after RBF displacement are permanently dropped from the mempool. They will not be included in any future block unless the original sender resubmits them.
2. **No notification**: Neither the node operator nor the transaction submitter receives any indication that the transaction was lost. The `debug!` log only fires before the failed call, not after.
3. **Inconsistent pool state**: The RBF replacement is recorded as successful (the new transaction enters the pool), but the displaced transactions that should have been recovered are silently gone. This creates an asymmetry: the RBF submitter's transaction is accepted, while the displaced parties' transactions vanish without recourse.

The most impactful failure mode is `Reject::Full`: if the verify queue is at capacity, every recovered transaction is dropped. Since the queue is bounded (`channel::bounded(24)` for the block channel; the verify queue has its own limit), this is a realistic condition under load. [2](#0-1) 

---

### Likelihood Explanation

**Realistic and attacker-reachable.** The entry path requires only the ability to submit transactions to the tx-pool — an unprivileged, externally reachable operation. An attacker can:

1. Flood the verify queue with many transactions to bring it near capacity (a normal tx-pool submitter can do this).
2. Submit an RBF transaction that displaces existing transactions from the pool.
3. The `may_recovered_txs` re-queue attempt fires inside `tokio::spawn` asynchronously. If the queue is full at that moment, `add_tx` returns `Reject::Full`, which is silently discarded.
4. The displaced transactions are permanently lost.

This does not require any privileged role, leaked key, or majority hashpower. It is reachable by any tx-pool submitter. [3](#0-2) 

---

### Recommendation

Replace `let _ = queue.add_tx(tx, false, None)` with explicit error handling. At minimum, log the failure with the transaction hash and rejection reason so operators can observe the loss. Ideally, implement a retry or fallback path:

```rust
for tx in may_recovered_txs {
    debug!("recover back: {:?}", tx.proposal_short_id());
    if let Err(reject) = queue.add_tx(tx.clone(), false, None) {
        warn!(
            "failed to re-queue recovered tx {} after RBF: {}",
            tx.hash(),
            reject
        );
        // optionally: record in recent_reject or surface via callback
    }
}
```

This mirrors the fix recommended in the original ERC20 report: check the return value of every transfer-like operation and handle failure explicitly rather than assuming success.

---

### Proof of Concept

1. Fill the tx-pool verify queue to near capacity by submitting many valid transactions via RPC (`send_transaction`).
2. Submit a new transaction that RBF-replaces one or more existing transactions in the pool (higher fee rate, overlapping inputs).
3. `submit_entry` calls `process_rbf`, which removes the conflicting transactions and returns them as `may_recovered_txs`.
4. The `tokio::spawn` closure attempts `queue.add_tx(tx, false, None)` for each recovered transaction.
5. Because the queue is full, `add_tx` returns `Err(Reject::Full)`.
6. `let _ =` discards the error. The recovered transactions are permanently gone from the mempool.
7. Observe via `get_pool_tx_detail_info` or `get_raw_tx_pool` that the displaced transactions are absent, with no rejection record in `get_transaction` or recent-reject storage. [1](#0-0)

### Citations

**File:** tx-pool/src/process.rs (L136-163)
```rust
                let may_recovered_txs = self.process_rbf(tx_pool, &entry, &conflicts);
                let evicted = _submit_entry(tx_pool, status, entry.clone(), &self.callbacks)?;

                // in a corner case, a tx with lower fee rate may be rejected immediately
                // after inserting into pool, return proper reject error here
                for evict in evicted {
                    let reject = Reject::Invalidated(format!(
                        "invalidated by tx {}",
                        evict.transaction().hash()
                    ));
                    self.callbacks.call_reject(tx_pool, &evict, reject);
                }

                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

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
