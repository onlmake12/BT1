### Title
TOCTOU Race in `resumeble_process_tx` Allows Duplicate Transaction Enqueue and Double CKB-VM Verification - (File: tx-pool/src/process.rs)

### Summary
`TxPoolService::resumeble_process_tx` performs duplicate-presence checks under separate, short-lived read locks and then enqueues the transaction under a separate write lock. Because each `.await` point releases the prior lock, a concurrent peer submission of the same transaction can pass all checks simultaneously and enqueue the same transaction into the verify queue multiple times, causing redundant and expensive CKB-VM script verification.

### Finding Description

`resumeble_process_tx` is the entry point for all remote peer transactions (`submit_remote_tx`) and notified transactions (`notify_tx`):

```
resumeble_process_tx
  → orphan_contains(&tx).await          // read lock on self.orphan, then released
  → verify_queue_contains(&tx).await    // read lock on self.verify_queue, then released
  → enqueue_verify_queue(tx, ...).await // write lock on self.verify_queue
``` [1](#0-0) 

Each of the three operations acquires and **releases** its own lock independently:

- `orphan_contains` acquires `self.orphan.read().await` and drops it before returning. [2](#0-1) 

- `verify_queue_contains` acquires `self.verify_queue.read().await` and drops it before returning. [3](#0-2) 

- `enqueue_verify_queue` acquires `self.verify_queue.write().await` as a completely separate operation. [4](#0-3) 

In Tokio's async runtime, every `.await` is a yield point where other tasks can be scheduled. The window between the `verify_queue_contains` check and the `enqueue_verify_queue` write is not protected by any held lock. Two concurrent tasks processing the same transaction can both observe the queue as empty and both proceed to enqueue.

The same structural problem exists in `process_tx` (the local RPC path):

```
process_tx
  → verify_queue_contains || orphan_contains  // separate read locks, released
  → _process_tx                               // pre_check (read lock) + verify_rtx (no lock) + submit_entry (write lock)
``` [5](#0-4) 

`_process_tx` calls `pre_check` under a read lock, then performs expensive CKB-VM verification with **no lock held**, then calls `submit_entry` under a write lock. Two concurrent calls for the same transaction can both pass `pre_check` and both run full script verification before the first one's write lock in `submit_entry` blocks the second. [6](#0-5) 

The final `submit_entry` → `_submit_entry` → `add_entry` does re-check for duplicates under the write lock: [7](#0-6) 

So the **pool state remains consistent**, but the duplicate CKB-VM verification work is already done and cannot be undone.

### Impact Explanation

The verify workers (`VerifyMgr`) pick tasks from the shared `verify_queue` and call `_process_tx`, which runs full CKB-VM script execution — the most CPU-intensive operation in the node: [8](#0-7) 

An attacker who causes the same transaction to be enqueued `N` times forces `N` full CKB-VM verifications. Since the maximum cycle limit per transaction is `max_block_cycles` (a consensus-level cap that can be very large), each duplicate verification can consume significant CPU time. With multiple workers and a crafted high-cycle transaction, this can saturate all verify workers, starving legitimate transactions from being admitted to the pool and delaying block assembly.

### Likelihood Explanation

The attack is straightforward for an unprivileged network peer:

1. Craft a valid transaction with a high cycle count (up to `max_block_cycles`).
2. Relay the same transaction to the target node from multiple peer connections simultaneously (or use a single peer that sends the same transaction in rapid succession before the first enqueue completes).
3. The concurrent `submit_remote_tx` → `resumeble_process_tx` calls race through the TOCTOU window.

No special privileges, keys, or majority hashpower are required. The attack is repeatable and can be sustained continuously. The node's async architecture (multiple Tokio tasks handling peer messages concurrently) makes the race window reliably triggerable under normal network load.

### Recommendation

Combine the duplicate check and the enqueue into a single critical section under the **write lock**:

```rust
// Atomic check-then-enqueue under a single write lock
async fn enqueue_verify_queue(...) -> Result<bool, Reject> {
    let mut queue = self.verify_queue.write().await;
    if queue.contains_key(&tx.proposal_short_id()) {
        return Err(Reject::Duplicated(tx.hash()));
    }
    // also check orphan under the same lock or use a combined lock
    queue.add_tx(tx, is_proposal_tx, remote)
}
```

Alternatively, ensure `add_tx` in `VerifyQueue` performs an idempotent insert (returning a distinct error on duplicate) and treat that error as the authoritative duplicate signal, removing the pre-checks in `resumeble_process_tx` entirely. The same pattern should be applied to `process_tx` — the `pre_check` read lock and the `submit_entry` write lock should either be merged or `pre_check` should be re-run inside the write lock before insertion.

### Proof of Concept

```
Peer A ──► submit_remote_tx(tx_high_cycles)
              └─► resumeble_process_tx
                    ├─ orphan_contains → false  (lock released)
                    ├─ verify_queue_contains → false  (lock released)
                    │                                   ← YIELD POINT
Peer B ──► submit_remote_tx(tx_high_cycles)
              └─► resumeble_process_tx
                    ├─ orphan_contains → false  (lock released)
                    ├─ verify_queue_contains → false  (lock released)
                    └─ enqueue_verify_queue → OK  (tx enqueued #1)
                    ← resumes
                    └─ enqueue_verify_queue → OK  (tx enqueued #2)

Worker 1: pop tx_high_cycles → full CKB-VM verify (N cycles)
Worker 2: pop tx_high_cycles → full CKB-VM verify (N cycles) [wasted]
```

Both workers complete verification; the second `submit_entry` is silently dropped by `add_entry`'s write-lock duplicate check, but the CPU cost of the second full verification is already paid. Repeating with many peers and maximum-cycle transactions exhausts all verify workers.

### Citations

**File:** tx-pool/src/process.rs (L237-240)
```rust
    pub(crate) async fn verify_queue_contains(&self, tx: &TransactionView) -> bool {
        let queue = self.verify_queue.read().await;
        queue.contains_key(&tx.proposal_short_id())
    }
```

**File:** tx-pool/src/process.rs (L242-245)
```rust
    pub(crate) async fn orphan_contains(&self, tx: &TransactionView) -> bool {
        let orphan = self.orphan.read().await;
        orphan.contains_key(&tx.proposal_short_id())
    }
```

**File:** tx-pool/src/process.rs (L335-353)
```rust
    pub(crate) async fn resumeble_process_tx(
        &self,
        tx: TransactionView,
        is_proposal_tx: bool,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<bool, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.orphan_contains(&tx).await {
            debug!("reject tx {} already in orphan pool", tx.hash());
            return Err(Reject::Duplicated(tx.hash()));
        }

        if self.verify_queue_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }
        self.enqueue_verify_queue(tx, is_proposal_tx, remote).await
    }
```

**File:** tx-pool/src/process.rs (L401-426)
```rust
    pub(crate) async fn process_tx(
        &self,
        tx: TransactionView,
        remote: Option<(Cycle, PeerIndex)>,
    ) -> Result<Completed, Reject> {
        // non contextual verify first
        self.non_contextual_verify(&tx, remote).await?;

        if self.verify_queue_contains(&tx).await || self.orphan_contains(&tx).await {
            return Err(Reject::Duplicated(tx.hash()));
        }

        if let Some((ret, snapshot)) = self
            ._process_tx(tx.clone(), remote.map(|r| r.0), None)
            .await
        {
            self.after_process(tx, remote, &snapshot, &ret).await;
            ret
        } else {
            // currently, the returned cycles is not been used, mock 0 if delay
            Ok(Completed {
                cycles: 0,
                fee: Capacity::zero(),
            })
        }
    }
```

**File:** tx-pool/src/process.rs (L705-753)
```rust
    pub(crate) async fn _process_tx(
        &self,
        tx: TransactionView,
        declared_cycles: Option<Cycle>,
        command_rx: Option<&mut watch::Receiver<ChunkCommand>>,
    ) -> Option<(Result<Completed, Reject>, Arc<Snapshot>)> {
        let wtx_hash = tx.witness_hash();
        let instant = Instant::now();
        let is_sync_process = command_rx.is_none();

        let (ret, snapshot) = self.pre_check(&tx).await;

        let (tip_hash, rtx, status, fee, tx_size) = try_or_return_with_snapshot!(ret, snapshot);

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = declared_cycles.unwrap_or_else(|| self.consensus.max_block_cycles());
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

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

**File:** tx-pool/src/component/pool_map.rs (L207-209)
```rust
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
```

**File:** tx-pool/src/verify_mgr.rs (L109-163)
```rust
    async fn process_inner(&mut self) {
        loop {
            if self.exit_signal.is_cancelled() {
                info!("Verify worker::process_inner exit_signal is cancelled");
                return;
            }
            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }
            // cheap query to check queue is not empty
            if self.tasks.read().await.is_empty() {
                return;
            }

            self.refresh_status();
            if self.status != ChunkCommand::Resume {
                return;
            }

            // pick a entry to run verify
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
    }
```
