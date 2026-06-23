### Title
Proposal-Transaction Priority in `VerifyQueue::peek` Causes Permanent Starvation of Regular User Transactions — (`tx-pool/src/component/verify_queue.rs`)

---

### Summary

The `VerifyQueue::peek` function unconditionally prioritizes any `is_proposal_tx` entry over all regular transactions, regardless of submission time. If proposal transactions are continuously added to the verify queue (e.g., via repeated block relay triggering `notify_tx`), regular user transactions already waiting in the queue can be permanently starved and never verified or admitted to the tx-pool.

---

### Finding Description

`VerifyQueue::peek` is the single dequeue selector used by all verify workers. Its logic is:

```rust
pub fn peek(&self, only_small_cycle: bool) -> Option<ProposalShortId> {
    let mut iter = self.inner.iter_by_added_time();

    if let Some(proposal_entry) = iter.find(|e| e.is_proposal_tx) {
        return Some(proposal_entry.inner.tx.proposal_short_id());
    }
    // regular tx selection follows only if NO proposal tx exists
    ...
}
``` [1](#0-0) 

The `find(|e| e.is_proposal_tx)` scan runs over the time-ordered index and returns the first proposal tx found. If **any** proposal tx is present in the queue, the function returns early and no regular tx is ever selected — by either worker role (`OnlySmallCycleTx` or `SubmitTimeFirst`), since both call `pop_front` which delegates to `peek`. [2](#0-1) 

The entry point that marks a transaction as a proposal tx is `notify_tx`:

```rust
pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
    self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
        .await
}
``` [3](#0-2) 

This calls `resumeble_process_tx` with `is_proposal_tx = true`, which enqueues the tx into the shared `VerifyQueue` with the proposal flag set. [4](#0-3) 

Workers are spawned in `VerifyMgr::new` — worker 0 is `OnlySmallCycleTx`, all others are `SubmitTimeFirst`. Both roles call `pop_front`, which calls `peek`. Since `peek` always returns a proposal tx first when one exists, **all workers** are diverted to proposal txs, leaving regular txs unprocessed. [5](#0-4) 

---

### Impact Explanation

A regular user submits a transaction to the tx-pool via RPC or P2P relay. The tx enters the `VerifyQueue`. If an attacker continuously relays blocks containing proposal sets that trigger `notify_tx` calls, the queue is kept populated with proposal txs. Because `peek` always selects a proposal tx first, the user's regular tx is never dequeued, never verified, and never admitted to the pool. The user's transaction is effectively stuck until the queue size limit is hit and it is evicted, or until the attacker stops — causing economic harm (missed epoch windows, time-sensitive transactions failing).

---

### Likelihood Explanation

Block relay is an unprivileged, externally reachable operation. Any peer can relay blocks. In CKB's two-phase commit model, proposals are a normal part of every block. An attacker operating as a miner (or relaying crafted blocks that pass header validation) can continuously produce blocks with proposal sets referencing arbitrary short IDs, triggering repeated `notify_tx` calls. The verify queue size limit (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE = 256 MB`) is large enough that an attacker can sustain the starvation condition for extended periods with modest transaction volume. [6](#0-5) 

---

### Recommendation

Replace the unconditional proposal-tx priority in `peek` with a **fair aging mechanism**: track how long each regular tx has been waiting, and after a configurable threshold (e.g., N milliseconds), promote it above pending proposal txs. Alternatively, cap the number of consecutive proposal-tx dequeues before a regular tx must be served (round-robin or weighted-fair-queuing between proposal and regular entries).

---

### Proof of Concept

1. User A submits a regular transaction `tx_user` via RPC → it enters `VerifyQueue` with `is_proposal_tx = false`, `added_time = T0`.
2. Attacker relays a stream of blocks each containing a proposal set. Each block triggers `notify_tx(tx_attacker_i)` → `enqueue_verify_queue(tx_attacker_i, true, None)`. Each `tx_attacker_i` enters the queue with `is_proposal_tx = true`.
3. Every call to `peek(false)` by any worker hits the `find(|e| e.is_proposal_tx)` branch and returns one of the attacker's proposal txs.
4. `tx_user` remains at its position in the `added_time` index but is never reached by `peek`, because the proposal-tx scan always succeeds first.
5. As long as the attacker maintains at least one proposal tx in the queue, `tx_user` is never dequeued, verified, or admitted to the pool — matching the FILO starvation pattern from the reference report. [1](#0-0) [7](#0-6)

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L180-194)
```rust
    pub fn peek(&self, only_small_cycle: bool) -> Option<ProposalShortId> {
        let mut iter = self.inner.iter_by_added_time();

        if let Some(proposal_entry) = iter.find(|e| e.is_proposal_tx) {
            return Some(proposal_entry.inner.tx.proposal_short_id());
        }

        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };

        entry.map(|e| e.inner.tx.proposal_short_id())
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L227-235)
```rust
        self.inner.insert(VerifyEntry {
            id: tx.proposal_short_id(),
            added_time: unix_time_as_millis(),
            inner: Entry { tx, remote },
            is_large_cycle,
            is_proposal_tx,
        });
        self.total_tx_size = total_tx_size;
        self.ready_rx.notify_one();
```

**File:** tx-pool/src/verify_mgr.rs (L130-145)
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
```

**File:** tx-pool/src/verify_mgr.rs (L179-203)
```rust
        let worker_num = service.tx_pool_config.max_tx_verify_workers;
        let workers: Vec<_> = (0..worker_num)
            .map({
                let tasks = Arc::clone(&service.verify_queue);
                let signal_exit = signal_exit.clone();
                move |idx| {
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
                    };
                    let (child_tx, child_rx) = watch::channel(ChunkCommand::Resume);
                    (
                        child_tx,
                        Worker::new(
                            service.clone(),
                            Arc::clone(&tasks),
                            child_rx,
                            signal_exit.clone(),
                            role,
                        ),
                    )
                }
            })
            .collect();
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

**File:** tx-pool/src/process.rs (L381-384)
```rust
    pub(crate) async fn notify_tx(&self, tx: TransactionView) -> Result<bool, Reject> {
        self.resumeble_process_tx_and_notify_full_reject(tx, true, None)
            .await
    }
```
