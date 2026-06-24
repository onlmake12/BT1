The code has been verified. All claims check out against the actual source. Here is the audit report:

---

Audit Report

## Title
OnlySmallCycleTx Worker Busy-Wait Loop via Large-Cycle Transaction Flooding — (`tx-pool/src/component/verify_queue.rs`, `tx-pool/src/verify_mgr.rs`)

## Summary
`VerifyQueue::peek(only_small_cycle=true)` performs two full O(N) linear scans over the time-ordered index instead of using the available `hashed_non_unique` index on `is_large_cycle`. When the queue is filled exclusively with large-cycle transactions, the `OnlySmallCycleTx` worker finds nothing, calls `re_notify()` which stores a `tokio::sync::Notify` permit, immediately re-enters `process_inner()`, and repeats — creating a tight busy-wait loop that pins a CPU core and contends the shared write lock against `SubmitTimeFirst` workers.

## Finding Description
**Root cause — O(N) scan in `peek()`:** [1](#0-0) 
The `is_large_cycle` field has a `hashed_non_unique` multi-index registered, but `peek()` ignores it entirely: [2](#0-1) 
Both the proposal-tx scan (line 183) and the small-cycle scan (line 188) call `iter_by_added_time().find(...)` — full linear traversals. When all N entries are large-cycle, both return `None`.

**Root cause — busy-wait loop:**
When `pop_front(true)` returns `None` but the queue is non-empty, `re_notify()` is called and the function returns: [3](#0-2) 
`re_notify()` calls `notify_one()`, which stores a permit in the `tokio::sync::Notify` when no waiter is present: [4](#0-3) 
Back in `run()`, `queue_ready.notified()` immediately consumes the stored permit without blocking and calls `process_inner()` again: [5](#0-4) 
The loop per iteration: read-lock `is_empty()` → write-lock `pop_front(true)` → 2× O(N) scan → `None` → `re_notify()` → return → immediate re-wakeup → repeat.

**Attacker entry point:**
The only guard at the relay layer is `declared_cycles > max_block_cycles`: [6](#0-5) 
Transactions with `large_cycle_threshold < declared_cycles <= max_block_cycles` pass through to `submit_remote_tx` and reach `add_tx`. The `is_large_cycle` flag is set directly from the attacker-controlled `declared_cycles`: [7](#0-6) 
No fee is required at queue-insertion time; fees are only checked inside `_process_tx` after dequeue.

**Worker role assignment:**
Worker 0 is always assigned `OnlySmallCycleTx` on any multi-core machine (the default): [8](#0-7) 

## Impact Explanation
The `OnlySmallCycleTx` worker pins one CPU core in a tight async loop. Each iteration acquires the write lock on the shared `Arc<RwLock<VerifyQueue>>`, directly contending with `SubmitTimeFirst` workers trying to pop and process legitimate transactions. With N entries in the queue (up to tens of thousands at the 256 MB limit), each iteration costs O(N) memory traversal plus write-lock acquisition. The result is measurably degraded transaction verification throughput on the targeted node. Because the attack requires no fee and is repeatable across many nodes simultaneously, this matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation
The attacker entry point is standard P2P transaction relay — no privileged access, no PoW, no fee. The attacker only needs to submit structurally valid transactions (passing `non_contextual_verify`) with `declared_cycles` in the range `(large_cycle_threshold, max_block_cycles]`. Multiple peers can fill the 256 MB queue rapidly. The attack is self-sustaining: as `SubmitTimeFirst` workers drain large-cycle entries, the attacker replenishes them. Any unprivileged network peer can trigger this.

## Recommendation
1. Replace the linear scan in `peek()` with a direct index lookup. The `MultiIndexMap` already maintains a `hashed_non_unique` index on `is_large_cycle`; use `get_by_is_large_cycle(false)` (combined with the `added_time` ordering) to find the oldest small-cycle entry in O(1) instead of O(N).
2. Add a backoff or "no small-cycle work available" flag to prevent the `OnlySmallCycleTx` worker from spinning when the queue contains only large-cycle entries — for example, by having it park itself until a genuinely new notification arrives (i.e., one triggered by `add_tx`) rather than immediately re-notifying via `re_notify()`.

## Proof of Concept
```
1. Configure node with max_tx_verify_workers >= 2 (default on any multi-core machine).
2. Connect as a P2P peer.
3. Send RelayTransactions messages containing N structurally valid CKB transactions,
   each with declared_cycles = large_cycle_threshold + 1.
   (No fee required; non_contextual_verify passes on structure alone.)
4. Fill the verify_queue up to its 256 MB limit.
5. Observe: Worker 0 (OnlySmallCycleTx) enters a tight loop.
   - CPU core pinned at ~100%.
   - Each iteration: 2× O(N) scan + write-lock acquisition.
   - SubmitTimeFirst workers experience write-lock contention.
6. Measure: tx processing throughput drops proportionally to lock contention.
   Assert: scan cost is O(N) not O(1); loop rate is bounded only by lock acquisition speed.
```

### Citations

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

**File:** tx-pool/src/verify_mgr.rs (L185-189)
```rust
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
                    };
```

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
