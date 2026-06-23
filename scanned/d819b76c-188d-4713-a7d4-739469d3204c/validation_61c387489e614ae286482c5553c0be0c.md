### Title
Silently Discarded `add_tx` Return Value Causes Permanent Transaction Loss During RBF Recovery - (File: `tx-pool/src/process.rs`)

---

### Summary

In `tx-pool/src/process.rs`, the return value of `queue.add_tx(tx, false, None)` is unconditionally discarded with `let _ = ...` during the RBF (Replace-By-Fee) recovery path. The `add_tx` function returns `Result<bool, Reject>` and can return `Err(Reject::Full(...))` when the verify queue is full. Silently ignoring this error causes recovered transactions to be permanently and silently dropped from the node, with no log, no notification, and no fallback — directly analogous to the unchecked ERC20 `transfer()` return value in the reference report.

---

### Finding Description

When an RBF replacement succeeds, `submit_entry` calls `process_rbf`, which identifies transactions that were previously conflicting with the replaced transaction but are no longer conflicting (because the new transaction uses different inputs). These "recovered" transactions are supposed to be re-added to the verify queue for re-verification and potential re-submission.

The recovery is done inside a `tokio::spawn` block:

```rust
// tx-pool/src/process.rs, lines 154–163
if !may_recovered_txs.is_empty() {
    let self_clone = self.clone();
    tokio::spawn(async move {
        // push the recovered txs back to verify queue, so that they can be verified and submitted again
        let mut queue = self_clone.verify_queue.write().await;
        for tx in may_recovered_txs {
            debug!("recover back: {:?}", tx.proposal_short_id());
            let _ = queue.add_tx(tx, false, None);  // ← return value silently discarded
        }
    });
}
``` [1](#0-0) 

The `add_tx` function signature is:

```rust
pub fn add_tx(
    &mut self,
    tx: TransactionView,
    is_proposal_tx: bool,
    remote: Option<(Cycle, PeerIndex)>,
) -> Result<bool, Reject>
``` [2](#0-1) 

It explicitly returns `Err(Reject::Full(...))` in two cases:

1. When `self.is_full(tx_size)` — the queue's total byte size is at or near `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`:

```rust
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
``` [3](#0-2) 

2. When `total_tx_size` overflows:

```rust
let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
    Reject::Full(format!(
        "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
        tx.hash()
    ))
})?;
``` [4](#0-3) 

In contrast, every other call site of `add_tx` (e.g., `enqueue_verify_queue`) properly propagates the `Result`:

```rust
async fn enqueue_verify_queue(...) -> Result<bool, Reject> {
    let mut queue = self.verify_queue.write().await;
    queue.add_tx(tx, is_proposal_tx, remote)
}
``` [5](#0-4) 

---

### Impact Explanation

When the verify queue is full and an RBF replacement triggers recovery, the recovered transactions are silently and permanently dropped from the node. There is no log message at `warn` or `error` level, no rejection callback, no notification to the original submitter, and no retry mechanism. The transactions vanish without trace. This breaks the correctness guarantee of the RBF recovery feature (introduced in CHANGELOG as `#4561: Recover possible transaction in conflicted cache when RBF`). [6](#0-5) 

A user whose transaction was removed from the pool due to RBF, and whose transaction was supposed to be recovered, would have no way to know it was silently dropped. Their transaction is permanently lost from the node's pool.

---

### Likelihood Explanation

The verify queue has a finite byte-size limit (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`). An unprivileged tx-pool submitter (reachable via the `send_transaction` RPC) can flood the verify queue with many transactions to fill it. Once full, any subsequent RBF replacement that triggers recovery will silently drop the recovered transactions. The attacker entry path is:

1. Flood the verify queue via repeated `send_transaction` RPC calls until `is_full()` returns true.
2. Submit a high-fee RBF replacement transaction that displaces an existing transaction with non-conflicting descendants.
3. The recovery path at line 161 silently discards the recovered transactions.

The `send_transaction` RPC is publicly accessible to any RPC caller. [7](#0-6) 

---

### Recommendation

Replace the silent discard with proper error handling. Log the failure and, if appropriate, propagate or retry:

```rust
for tx in may_recovered_txs {
    debug!("recover back: {:?}", tx.proposal_short_id());
    if let Err(e) = queue.add_tx(tx.clone(), false, None) {
        error!(
            "Failed to recover tx {} after RBF: {}",
            tx.hash(), e
        );
    }
}
```

This mirrors the pattern used elsewhere in the codebase where channel send failures are logged at `error!` level rather than silently discarded. [8](#0-7) 

---

### Proof of Concept

1. Configure a CKB node with RBF enabled (`min_rbf_rate > min_fee_rate`).
2. Submit enough large transactions via `send_transaction` RPC to fill the verify queue (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`).
3. Submit `tx_A` spending cellbase output `O`.
4. Submit `tx_B` (child of `tx_A`) spending `tx_A`'s output.
5. Submit `tx_C` (RBF replacement of `tx_A`) with a higher fee, using the same input `O` but different outputs — so `tx_B` is no longer conflicting and should be recovered.
6. Observe: `tx_B` is silently dropped. No error is logged. `tx_B` is not in the pool, not in the orphan pool, and not in the verify queue. The submitter of `tx_B` receives no notification.

The root cause is at: [9](#0-8)

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

**File:** tx-pool/src/process.rs (L673-677)
```rust
    pub(crate) fn send_result_to_relayer(&self, result: TxVerificationResult) {
        if let Err(e) = self.tx_relay_sender.send(result) {
            error!("tx-pool tx_relay_sender internal error {}", e);
        }
    }
```

**File:** tx-pool/src/process.rs (L860-868)
```rust
    async fn enqueue_verify_queue(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let mut queue = self.verify_queue.write().await;
        queue.add_tx(tx, is_proposal_tx, remote)
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L198-203)
```rust
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
```

**File:** tx-pool/src/component/verify_queue.rs (L215-220)
```rust
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
```

**File:** tx-pool/src/component/verify_queue.rs (L221-226)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
```

**File:** CHANGELOG.md (L218-218)
```markdown
- #4561: Recover possible transaction in conflicted cache when RBF (@chenyukang)
```

**File:** rpc/src/module/pool.rs (L612-635)
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
    }
```
