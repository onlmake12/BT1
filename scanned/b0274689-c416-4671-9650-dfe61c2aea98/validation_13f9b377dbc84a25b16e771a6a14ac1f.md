Now I have all the code needed to analyze this claim. Let me trace through the exact execution path.

I have all the code needed for a complete analysis. Let me trace the exact execution path.

**Execution trace:**

**Entry point** (`sync/src/relayer/transactions_process.rs`): A remote peer sends a `RelayTransactions` message. The only guard on `declared_cycles` at this layer is `declared_cycles > max_block_cycles` (consensus max, very large). Transactions with `large_cycle_threshold < declared_cycles <= max_block_cycles` pass through and reach `submit_remote_tx`. [1](#0-0) 

**Queue insertion** (`tx-pool/src/process.rs`): `submit_remote_tx` → `resumeble_process_tx` → `non_contextual_verify` (structural only, no fee check) → `enqueue_verify_queue` → `add_tx`. The `is_large_cycle` flag is set purely from the attacker-controlled `declared_cycles`. [2](#0-1) 

**The O(N) scan** (`peek(only_small_cycle=true)`): When all entries are large-cycle, the function performs two full linear scans: first for proposal txs, then for non-large-cycle txs. Both return nothing. Critically, although `is_large_cycle` has a `hashed_non_unique` index registered on the `MultiIndexMap`, `peek()` ignores it entirely and uses `iter_by_added_time().find(...)` instead. [3](#0-2) [4](#0-3) 

**The tight loop** (`process_inner`): When `pop_front(true)` returns `None` but the queue is non-empty, `re_notify()` is called. `re_notify()` calls `self.ready_rx.notify_one()`, which stores a permit in the `tokio::sync::Notify`. The worker returns from `process_inner()`, immediately re-enters `queue_ready.notified()` in `run()`, consumes the stored permit without blocking, and calls `process_inner()` again. This is a busy-wait loop. [5](#0-4) [6](#0-5) [7](#0-6) 

**Worker role assignment**: Worker 0 gets `OnlySmallCycleTx` whenever `worker_num > 1`, which is the default (3/4 of CPU cores). [8](#0-7) 

---

### Title
OnlySmallCycleTx Worker Busy-Wait Loop via Large-Cycle Transaction Flooding — (`tx-pool/src/component/verify_queue.rs`, `tx-pool/src/verify_mgr.rs`)

### Summary
When the verify queue is filled exclusively with large-cycle transactions (declared_cycles > `large_cycle_threshold`), the `OnlySmallCycleTx` worker enters a tight busy-wait loop: it performs two O(N) linear scans per wakeup, finds nothing, calls `re_notify()` which immediately re-arms its own wakeup, and repeats indefinitely. The `is_large_cycle` hash index on the `MultiIndexMap` is never consulted by `peek()`, making the scan unavoidably O(N).

### Finding Description
`VerifyQueue::peek(only_small_cycle=true)` at line 188 uses `self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)` — a full linear scan over the time-ordered index — even though `is_large_cycle` is declared as `#[multi_index(hashed_non_unique)]` and a direct O(1) lookup via `get_by_is_large_cycle(false)` is available. When all N entries are large-cycle, both the proposal-tx scan (line 183) and the small-cycle scan (line 188) traverse the entire queue and return `None`.

In `Worker::process_inner()`, when `pop_front(true)` returns `None` but `is_empty()` is false, `tasks.re_notify()` is called. Because `tokio::sync::Notify::notify_one()` stores a permit when no waiter is present, the worker immediately re-enters `process_inner()` on the next `select!` iteration without any sleep or backoff. The loop per iteration is:

1. Acquire read lock → `is_empty()` check
2. Acquire write lock → `pop_front(true)` → 2× O(N) scan → `None`
3. `re_notify()` → stores permit
4. Release write lock, return
5. `queue_ready.notified()` → consumes stored permit immediately
6. Goto 1

### Impact Explanation
The `OnlySmallCycleTx` worker consumes a full CPU core in a tight async loop. Each iteration acquires the write lock on the shared `Arc<RwLock<VerifyQueue>>`, which directly contends with `SubmitTimeFirst` workers trying to pop and process legitimate transactions. With N entries in the queue, each iteration costs O(N) memory traversal. At the 256MB queue limit with minimal-size transactions, N can reach tens of thousands. The result is: elevated CPU usage on the targeted node, write-lock starvation of `SubmitTimeFirst` workers, and measurably degraded transaction verification throughput.

### Likelihood Explanation
The attacker entry point is standard P2P transaction relay. No privileged access, no PoW, no fee payment at queue-insertion time (fees are checked only during actual verification inside `_process_tx`). The attacker only needs to submit structurally valid transactions (passing `non_contextual_verify`) with `declared_cycles` in the range `(large_cycle_threshold, max_block_cycles]`. Multiple peers can be used to fill the 256MB queue faster. The attack is self-sustaining as long as the attacker keeps submitting new transactions to replace those drained by `SubmitTimeFirst` workers.

### Recommendation
Replace the linear scan in `peek()` with a direct index lookup. The `MultiIndexMap` already maintains a `hashed_non_unique` index on `is_large_cycle`; use `get_by_is_large_cycle(false)` (or the equivalent ordered lookup) to find the oldest small-cycle entry in O(1)/O(log N) instead of O(N). Additionally, add a backoff or a "no small-cycle work available" flag to prevent the `OnlySmallCycleTx` worker from spinning when the queue contains only large-cycle entries — for example, by having it park itself until a new notification arrives rather than immediately re-notifying.

### Proof of Concept
```
1. Configure node with max_tx_verify_workers >= 2 (default on any multi-core machine).
2. Connect as a P2P peer.
3. Send RelayTransactions messages containing N structurally valid CKB transactions,
   each with declared_cycles = large_cycle_threshold + 1.
   (No fee required; non_contextual_verify passes on structure alone.)
4. Fill the verify_queue up to its 256MB limit.
5. Observe: Worker 0 (OnlySmallCycleTx) enters a tight loop.
   - CPU core pinned at ~100%.
   - Each iteration: 2× O(N) scan + write-lock acquisition.
   - SubmitTimeFirst workers experience write-lock contention.
6. Measure: tx processing throughput drops proportionally to lock contention.
   Assert: scan cost is O(N) not O(1); loop rate is bounded only by lock acquisition speed.
```

### Citations

**File:** sync/src/relayer/transactions_process.rs (L63-74)
```rust
        let max_block_cycles = self.relayer.shared().consensus().max_block_cycles();
        if txs
            .iter()
            .any(|(_, declared_cycles)| declared_cycles > &max_block_cycles)
        {
            self.nc.ban_peer(
                self.peer,
                DEFAULT_BAN_TIME,
                String::from("relay declared cycles greater than max_block_cycles"),
            );
            return Status::ok();
        }
```

**File:** tx-pool/src/component/verify_queue.rs (L46-47)
```rust
    #[multi_index(hashed_non_unique)]
    is_large_cycle: bool,
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

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
```

**File:** tx-pool/src/component/verify_queue.rs (L239-242)
```rust
    /// When OnlySmallCycleTx Worker is wakeup, but found the tx is large cycle tx, notify other workers.
    pub fn re_notify(&self) {
        self.ready_rx.notify_one();
    }
```

**File:** tx-pool/src/verify_mgr.rs (L86-103)
```rust
    async fn run(mut self) {
        let queue_ready = self.tasks.read().await.subscribe();
        self.refresh_status();
        loop {
            tokio::select! {
                _ = self.exit_signal.cancelled() => {
                    break;
                }
                _ = self.command_rx.changed() => {
                    self.status = self.command_rx.borrow_and_update().to_owned();
                    self.process_inner().await;
                }
                _ = queue_ready.notified() => {
                    self.process_inner().await;
                }
            };
        }
    }
```

**File:** tx-pool/src/verify_mgr.rs (L130-143)
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
```

**File:** tx-pool/src/verify_mgr.rs (L179-189)
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
```
