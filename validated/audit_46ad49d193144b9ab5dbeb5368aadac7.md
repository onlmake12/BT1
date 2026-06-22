### Title
Transaction Silently Dropped from Verify Queue When Processing Is Interrupted — (`tx-pool/src/verify_mgr.rs`)

### Summary

In `tx-pool/src/verify_mgr.rs`, a transaction entry is unconditionally removed from the `VerifyQueue` via `pop_front` **before** `_process_tx` completes. If `_process_tx` returns `None` (which occurs when a `Suspend`/`Stop` chunk command is received mid-verification — a normal occurrence during block processing), the transaction is silently discarded with no re-queuing, no rejection callback, and no notification to the submitter or relayer. This is structurally identical to the HolographOperator pattern: state is deleted before the operation succeeds, and failure leaves the item irrecoverable from the queue.

---

### Finding Description

In `Worker::process_inner` (`tx-pool/src/verify_mgr.rs`):

```rust
// pick a entry to run verify
let entry = {
    let mut tasks = self.tasks.write().await;
    match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
        Some(entry) => entry,   // <-- entry removed from queue HERE
        None => { ... return; }
    }
};

if let Some((res, snapshot)) = self
    .service
    ._process_tx(entry.tx.clone(), entry.remote.map(|e| e.0), Some(&mut self.command_rx))
    .await
{
    self.service.after_process(entry.tx, entry.remote, &snapshot, &res).await;
} else {
    info!("_process_tx for tx: {} returned none", entry.tx.hash());
    // entry is dropped here — no re-queue, no rejection, no callback
}
``` [1](#0-0) 

The `pop_front` call at line 132 removes the entry from the shared `VerifyQueue` under a write lock. The entry is now owned exclusively by this worker. `_process_tx` is then called with `Some(&mut self.command_rx)`, which allows it to observe chunk commands mid-execution. When the chain service begins processing a new block, it sends a `Suspend` command via `tx_pool_controller.suspend_chunk_process()`: [2](#0-1) 

This causes `_process_tx` to return `None`. At that point:

- The transaction is **not** in the `VerifyQueue` (already popped).
- The transaction is **not** in the pending pool (never added).
- The transaction is **not** in the orphan pool.
- `after_process` is **not** called, so no result is sent to the relayer and no rejection event is emitted.
- The entry is simply dropped. [3](#0-2) 

The chain service's suspend/resume cycle is: [4](#0-3) 

Every block processed by `ConsumeUnverifiedBlocks` triggers a `suspend_chunk_process` → `continue_chunk_process` pair, creating a window during which any in-flight `_process_tx` call may return `None`.

---

### Impact Explanation

Any transaction that is mid-verification when a block is processed is silently evicted from the node's tx-pool pipeline with no trace. The submitting peer or RPC caller receives no rejection notification. The transaction is not re-queued for retry. From the node's perspective the transaction has vanished; the user must detect this externally (e.g., by polling `get_transaction`) and resubmit. Under high block-processing load (e.g., IBD catch-up or rapid block arrival), this can affect many transactions per block. Transactions with time-sensitive proposal windows (CKB uses a two-phase proposal/commit cycle) may miss their proposal window entirely if silently dropped and not resubmitted in time.

---

### Likelihood Explanation

The trigger is **normal chain operation**: every block processed by `ConsumeUnverifiedBlocks` issues a `suspend_chunk_process`. Any transaction that happens to be inside `_process_tx` at that moment is dropped. This is not a rare edge case — it is a structural race between block processing and tx verification that occurs continuously during normal node operation. Any tx-pool submitter (RPC `send_transaction`, relay peer) can be affected without any special action.

---

### Recommendation

The fix mirrors the recommended mitigation in the external report: do not remove the entry from the queue until processing has definitively succeeded or failed. Options:

1. **Re-queue on `None`**: If `_process_tx` returns `None`, push the entry back to the front of the `VerifyQueue` so it is retried on the next `Resume` command.
2. **Peek-then-remove**: Hold the entry in the queue (mark it as "in-flight") and only remove it after `after_process` completes, similar to how Arbitrum retryable tickets work.
3. **Emit rejection on `None`**: At minimum, call `send_result_to_relayer` with a `Reject` result so the submitter is notified and can resubmit.

```rust
} else {
    info!("_process_tx for tx: {} returned none, re-queuing", entry.tx.hash());
    // Re-add to front of queue for retry on next Resume
    self.tasks.write().await.push_front(entry);
}
```

---

### Proof of Concept

1. Submit a large-cycle transaction via RPC `send_transaction`. It enters the `VerifyQueue`.
2. A worker calls `pop_front`, removing it from the queue.
3. A peer sends a valid block; `ConsumeUnverifiedBlocks` calls `suspend_chunk_process`.
4. `_process_tx` observes the `Suspend` command and returns `None`.
5. The worker logs `"_process_tx for tx: <hash> returned none"` and drops the entry.
6. Query `get_transaction(<hash>)` — returns `null`. The transaction is gone from the node with no rejection recorded in `recent_reject`.
7. The submitter must resubmit; if the transaction had a proposal-window deadline, it may be too late. [1](#0-0) [4](#0-3)

### Citations

**File:** tx-pool/src/verify_mgr.rs (L130-162)
```rust
            let entry = {
                let mut tasks = self.tasks.write().await;
                match tasks.pop_front(self.role == WorkerRole::OnlySmallCycleTx) {
                    Some(entry) => entry,
                    None => {
                        if !tasks.is_empty() {
                            tasks.re_notify();
                            debug!(
                                "Worker (role: {:?}) didn't got tx after pop_front, but tasks is not empty, notify other Workers now",
                                self.role
                            );
                        }
                        return;
                    }
                }
            };

            if let Some((res, snapshot)) = self
                .service
                ._process_tx(
                    entry.tx.clone(),
                    entry.remote.map(|e| e.0),
                    Some(&mut self.command_rx),
                )
                .await
            {
                self.service
                    .after_process(entry.tx, entry.remote, &snapshot, &res)
                    .await;
            } else {
                info!("_process_tx for tx: {} returned none", entry.tx.hash());
            }
        }
```

**File:** chain/src/verify.rs (L81-101)
```rust
                        let _ = self.tx_pool_controller.suspend_chunk_process();

                        let _trace_now = minstant::Instant::now();
                        let block_hash = unverified_task.block.hash();
                        let block_number = unverified_task.block.number();
                        if let Err(payload) = catch_unwind(AssertUnwindSafe(|| {
                            self.processor.consume_unverified_blocks(unverified_task);
                        })) {
                            error!(
                                "consume unverified block {}-{} panicked: {}",
                                block_number,
                                block_hash,
                                panic_payload_to_string(payload.as_ref())
                            );
                            self.processor.is_pending_verify.remove(&block_hash);
                        }
                        if let Some(handle) = ckb_metrics::handle() {
                            handle.ckb_chain_consume_unverified_block_duration.observe(_trace_now.elapsed().as_secs_f64())
                        }

                        let _ = self.tx_pool_controller.continue_chunk_process();
```
