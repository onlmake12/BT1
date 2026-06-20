### Title
Unchecked Return Value of `queue.add_tx` in RBF Recovery Path Causes Silent Transaction Loss — (File: `tx-pool/src/process.rs`)

---

### Summary

In `tx-pool/src/process.rs`, the `submit_entry` function spawns an async task to recover RBF-replaced transactions back into the verify queue. The call `let _ = queue.add_tx(tx, false, None)` explicitly discards the `Result`, meaning any failure (e.g., queue at capacity) is silently swallowed with no error log, no retry, and no caller notification. Affected transactions are permanently and invisibly dropped from the pool.

---

### Finding Description

Inside `submit_entry`, after an RBF replacement is processed, the displaced transactions are supposed to be re-queued for verification: [1](#0-0) 

```rust
if !may_recovered_txs.is_empty() {
    let self_clone = self.clone();
    tokio::spawn(async move {
        let mut queue = self_clone.verify_queue.write().await;
        for tx in may_recovered_txs {
            debug!("recover back: {:?}", tx.proposal_short_id());
            let _ = queue.add_tx(tx, false, None);   // ← error silently discarded
        }
    });
}
```

`add_tx` returns a `Result`. Using `let _ = ...` unconditionally discards that result. If `add_tx` returns `Err` (e.g., the verify queue is at capacity, or the entry is a duplicate after a race), the displaced transaction is permanently removed from the pool with:

- No error log emitted.
- No retry or fallback path.
- No signal to the original submitter.

The `add_entry` / `record_entry_edges` path that `add_tx` ultimately calls does return meaningful errors (e.g., `Reject::RBFRejected` for double-spend conflicts): [2](#0-1) 

Those errors are properly propagated everywhere else in the codebase, but not here.

---

### Impact Explanation

An RBF-replaced transaction that is supposed to be recovered is silently and permanently evicted from the pool. The pool's internal state (edges, links, size counters) was already updated to remove the displaced transaction; if re-insertion fails, the pool is left in an inconsistent state where the transaction no longer exists anywhere. Miners lose the fee opportunity; the original sender's transaction vanishes without any rejection notice. Under adversarial queue-flooding, an attacker can deterministically trigger this path to cause targeted transaction loss.

---

### Likelihood Explanation

Any RPC caller can submit a valid RBF transaction via `send_transaction`: [3](#0-2) 

The verify queue has a finite capacity. Under sustained load — or after an attacker deliberately fills the queue with cheap transactions — the queue will be full precisely when the recovery task runs. Because the spawn is fire-and-forget and the lock is acquired asynchronously, there is no ordering guarantee between queue-filling and recovery. This is a realistic condition on a busy mainnet node.

---

### Recommendation

Replace the silent discard with explicit error handling:

```rust
if let Err(e) = queue.add_tx(tx, false, None) {
    error!(
        "Failed to recover RBF-displaced tx {:?} back into verify queue: {}",
        tx.proposal_short_id(), e
    );
}
```

Additionally, consider whether a full verify queue should block or delay the RBF acceptance rather than silently dropping the displaced transactions.

---

### Proof of Concept

1. Flood the node's verify queue to capacity with low-fee transactions via repeated `send_transaction` RPC calls.
2. Submit a valid RBF transaction (higher fee, same inputs) that displaces a target transaction already in the pool.
3. `submit_entry` calls `process_rbf`, populates `may_recovered_txs` with the displaced transaction, and spawns the recovery task.
4. The recovery task acquires the queue write-lock and calls `queue.add_tx(displaced_tx, false, None)`.
5. Because the queue is full, `add_tx` returns `Err`; `let _ = ...` discards it.
6. The displaced transaction is permanently gone from the pool — no rejection event, no re-broadcast, no log entry.

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

**File:** tx-pool/src/component/edges.rs (L33-53)
```rust
    pub(crate) fn insert_input(
        &mut self,
        out_point: OutPoint,
        txid: ProposalShortId,
    ) -> Result<(), Reject> {
        // inputs is occupied means double speanding happened here
        match self.inputs.entry(out_point.clone()) {
            Entry::Occupied(occupied) => {
                let msg = format!(
                    "txpool unexpected double-spending out_point: {:?} old_tx: {:?} new_tx: {:?}",
                    out_point,
                    occupied.get(),
                    txid
                );
                Err(Reject::RBFRejected(msg))
            }
            Entry::Vacant(vacant) => {
                vacant.insert(txid);
                Ok(())
            }
        }
```

**File:** rpc/src/module/pool.rs (L612-634)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
```
