### Title
Silent Loss of RBF-Recovered Transactions When Verify Queue Is Full — (File: tx-pool/src/process.rs)

---

### Summary

In `submit_entry` (`tx-pool/src/process.rs`), when an RBF transaction succeeds and there are recovered conflicting transactions (`may_recovered_txs`), those transactions are pushed back into the `VerifyQueue` without first checking whether the queue has capacity to accept them. If the queue is at its 256 MB hard limit, the recovered transactions are silently discarded — no rejection notification is sent to the relayer, and the original senders receive no signal that their transactions have been permanently dropped.

---

### Finding Description

After a successful RBF submission, `submit_entry` spawns a task to re-enqueue recovered transactions:

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
``` [1](#0-0) 

`VerifyQueue::add_tx` returns `Err(Reject::Full(...))` when the queue's running `total_tx_size` would exceed `DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE` (256 MB):

```rust
if self.is_full(tx_size) {
    return Err(Reject::Full(format!(
        "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
        tx.hash()
    )));
}
``` [2](#0-1) 

The constant and the `is_full` predicate: [3](#0-2) [4](#0-3) 

By contrast, every other entry point into the verify queue (`submit_remote_tx`, `notify_tx`, `enqueue_verify_queue` for high-cycle orphans) propagates the `Reject::Full` error back to the caller and notifies the relayer: [5](#0-4) 

The recovered-transaction path is the only one that swallows the error entirely.

---

### Impact Explanation

Transactions that were previously rejected as RBF conflicts and placed in the conflicts pool are supposed to be re-verified once the conflicting transaction is replaced. If the verify queue is full at that moment, they are permanently removed from every pool (main pool, orphan pool, verify queue, conflicts pool) with no trace and no notification. Their senders observe no pool entry and receive no rejection signal, so they cannot distinguish "still pending" from "permanently dropped." For time-sensitive transactions (e.g., those with `since` constraints) this silent loss is irreversible within the valid window.

**Impact: Medium** — funds are not stolen, but legitimate transactions are permanently lost without recourse unless the sender actively polls and resubmits.

---

### Likelihood Explanation

RBF is enabled by default on mainnet when `min_rbf_rate > min_fee_rate` (default config: 1 500 vs 1 000 shannons/KB). [6](#0-5) 

An unprivileged tx-pool submitter can:
1. Continuously submit large transactions (each up to `TRANSACTION_SIZE_LIMIT` = 512 KB) to keep the verify queue near its 256 MB ceiling.
2. Arrange for a victim transaction to be recorded in the conflicts pool (by submitting a conflicting transaction that fails with `RBFRejected`).
3. Submit an RBF replacement that succeeds, triggering the recovery path while the queue is full.

The verify queue is drained by background workers, so the attacker must sustain the flood, but this is achievable with modest resources given the 256 MB ceiling is ~1.4× the 180 MB tx-pool limit.

**Likelihood: Medium.**

---

### Recommendation

Before spawning the recovery task, check whether the verify queue has room. If it does not, send a `TxVerificationResult::Reject` notification for each dropped transaction so the relayer can mark them as unknown and their senders can resubmit:

```rust
if !may_recovered_txs.is_empty() {
    let self_clone = self.clone();
    tokio::spawn(async move {
        let mut queue = self_clone.verify_queue.write().await;
        for tx in may_recovered_txs {
            let tx_hash = tx.hash();
            if let Err(_reject) = queue.add_tx(tx, false, None) {
                self_clone.send_result_to_relayer(
                    TxVerificationResult::Reject { tx_hash }
                );
            }
        }
    });
}
```

This mirrors the pattern already used in `resumeble_process_tx_and_notify_full_reject`: [5](#0-4) 

---

### Proof of Concept

1. **Fill the verify queue**: submit a stream of fee-paying transactions totalling ≥ 255 999 999 bytes in the verify queue's `total_tx_size` counter.
2. **Create a conflict entry**: submit Tx A that spends the same input as Tx B (already in the pool). Tx A fails with `RBFRejected` (fee rate too low) and is recorded in the conflicts pool via `record_conflict`.
3. **Trigger RBF**: submit Tx C, a valid RBF replacement of Tx B with a sufficiently higher fee rate. `submit_entry` succeeds; `process_rbf` returns Tx A in `may_recovered_txs`.
4. **Silent drop**: the spawned task calls `queue.add_tx(tx_a, false, None)`, which returns `Err(Reject::Full(...))`. The error is discarded with `let _`. Tx A is now in no pool and no rejection is sent.
5. **Observe**: `get_transaction(tx_a_hash)` returns `status: unknown`. The sender of Tx A receives no rejection notification and must discover the loss by polling. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/process.rs (L355-369)
```rust
    async fn resumeble_process_tx_and_notify_full_reject(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        let tx_hash = tx.hash();
        let ret = self.resumeble_process_tx(tx, is_proposal_tx, remote).await;

        if matches!(ret, Err(Reject::Full(_))) {
            self.send_result_to_relayer(TxVerificationResult::Reject { tx_hash });
        }

        ret
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L103-106)
```rust
    /// Returns true if the queue is full.
    pub fn is_full(&self, add_tx_size: usize) -> bool {
        add_tx_size >= DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE - self.total_tx_size
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L196-237)
```rust
    /// If the queue did not have this tx present, true is returned.
    /// If the queue did have this tx present, false is returned.
    pub fn add_tx(
        &mut self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        if self.contains_key(&tx.proposal_short_id()) {
            if is_proposal_tx {
                self.remove_tx(&tx.proposal_short_id());
            } else {
                return Ok(false);
            }
        }
        let tx_size = tx.data().serialized_size_in_block();
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
        if self.is_full(tx_size) {
            return Err(Reject::Full(format!(
                "verify_queue total_tx_size exceeded, failed to add tx: {:#x}",
                tx.hash()
            )));
        }
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "verify_queue total_tx_size overflowed, failed to add tx: {:#x}",
                tx.hash()
            ))
        })?;
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
        Ok(true)
    }
```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```
