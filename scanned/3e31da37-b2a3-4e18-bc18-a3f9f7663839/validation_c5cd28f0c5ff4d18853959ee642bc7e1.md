All three key files are confirmed. Let me verify the `submit_remote_tx` → `enqueue_verify_queue` path as well.

Audit Report

## Title
OnlySmallCycleTx Worker Busy-Wait Loop via Large-Cycle Transaction Flooding — (`tx-pool/src/component/verify_queue.rs`, `tx-pool/src/verify_mgr.rs`)

## Summary
`VerifyQueue::peek(only_small_cycle=true)` performs two full O(N) linear scans over the time-ordered index instead of using the available `hashed_non_unique` index on `is_large_cycle`. When the queue is filled exclusively with large-cycle transactions, the `OnlySmallCycleTx` worker finds nothing, calls `re_notify()` which immediately re-arms its own wakeup via `tokio::sync::Notify::notify_one()`, and re-enters `process_inner()` without any sleep or backoff — creating a tight busy-wait loop that pins a CPU core and contends on the shared write lock.

## Finding Description
**Root cause — linear scan ignoring available index:**

`peek()` at `verify_queue.rs:180-194` uses `self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)` for the small-cycle path, even though `is_large_cycle` is declared with `#[multi_index(hashed_non_unique)]` at line 46-47, making a direct O(1) lookup available. When all N entries are large-cycle, both the proposal-tx scan (line 183) and the small-cycle scan (line 188) traverse the entire queue and return `None`. [1](#0-0) [2](#0-1) 

**Root cause — `re_notify()` stores a permit, causing immediate re-wakeup:**

In `process_inner()` at `verify_mgr.rs:130-143`, when `pop_front(true)` returns `None` but `is_empty()` is false, `tasks.re_notify()` is called. `re_notify()` calls `self.ready_rx.notify_one()`, which stores a permit in the `tokio::sync::Notify` when no waiter is currently parked. [3](#0-2) [4](#0-3) 

**The tight loop in `run()`:**

`run()` at `verify_mgr.rs:86-103` calls `process_inner()` on each `queue_ready.notified()` event. Because `re_notify()` stores a permit before the worker re-parks, `queue_ready.notified()` resolves immediately on the next `select!` iteration. The loop per iteration is: (1) read-lock `is_empty()` → false; (2) write-lock `pop_front(true)` → 2× O(N) scan → `None`; (3) `re_notify()` stores permit; (4) return from `process_inner()`; (5) `notified()` consumes stored permit immediately; (6) goto 1. [5](#0-4) 

**Worker role assignment:**

Worker 0 is unconditionally assigned `OnlySmallCycleTx` whenever `worker_num > 1`, which is the default on any multi-core machine. [6](#0-5) 

**Attacker-controlled `is_large_cycle` flag:**

The `is_large_cycle` flag is derived solely from the peer-supplied `declared_cycles` value, with no independent verification at queue-insertion time. [7](#0-6) 

**Entry point:**

`transactions_process.rs:63-74` only rejects `declared_cycles > max_block_cycles`. Transactions with `large_cycle_threshold < declared_cycles <= max_block_cycles` pass through to `submit_remote_tx` and reach `add_tx`. The attacker must first announce tx hashes via `RelayTransactionHashes` to get the node to request them, then deliver the actual transactions with inflated `declared_cycles`. [8](#0-7) 

## Impact Explanation
The `OnlySmallCycleTx` worker consumes a full CPU core in a tight async loop. Each iteration acquires the write lock on the shared `Arc<RwLock<VerifyQueue>>`, directly contending with `SubmitTimeFirst` workers trying to pop and process legitimate transactions. With N entries in the queue (up to the 256MB limit), each iteration costs O(N) memory traversal. The result is elevated CPU usage, write-lock starvation of `SubmitTimeFirst` workers, and measurably degraded transaction verification throughput on the targeted node. This matches **High: Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, as the attack requires no fee payment at queue-insertion time and can be sustained cheaply across multiple targeted nodes.

## Likelihood Explanation
The attacker entry point is standard P2P transaction relay. No privileged access, no PoW, and no fee payment is required at queue-insertion time (fees are only checked during actual verification inside `_process_tx`). The attacker needs to: (1) connect as a P2P peer; (2) announce transaction hashes via `RelayTransactionHashes` to trigger the node's `GetRelayTransactions` request; (3) respond with structurally valid transactions carrying `declared_cycles` in `(large_cycle_threshold, max_block_cycles]`. The attack is self-sustaining as long as the attacker keeps replenishing the queue. Multiple peers can be used to fill the 256MB queue faster.

## Recommendation
1. **Replace the linear scan in `peek()`** with a direct index lookup. The `MultiIndexMap` already maintains a `hashed_non_unique` index on `is_large_cycle`; use `get_by_is_large_cycle(false)` (combined with the `added_time` ordering) to find the oldest small-cycle entry in O(1)/O(log N) instead of O(N).
2. **Add backoff or a "no small-cycle work" flag** to prevent the `OnlySmallCycleTx` worker from spinning when the queue contains only large-cycle entries. For example, have it park itself until a new notification arrives (i.e., do not call `re_notify()` when the worker's own role is `OnlySmallCycleTx` and all entries are large-cycle) — instead, let the next `add_tx()` call trigger the wakeup.

## Proof of Concept
```
1. Configure a CKB node with max_tx_verify_workers >= 2 (default on any multi-core machine).
2. Connect as a P2P peer.
3. Send RelayTransactionHashes with N tx hashes to trigger the node's GetRelayTransactions.
4. Respond with N structurally valid CKB transactions, each with
   declared_cycles = large_cycle_threshold + 1.
   (No fee required; non_contextual_verify passes on structure alone.)
5. Fill the verify_queue up to its 256MB limit.
6. Observe: Worker 0 (OnlySmallCycleTx) enters a tight loop.
   - CPU core pinned at ~100%.
   - Each iteration: 2× O(N) scan + write-lock acquisition.
   - SubmitTimeFirst workers experience write-lock contention.
7. Measure: tx processing throughput drops proportionally to lock contention.
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
