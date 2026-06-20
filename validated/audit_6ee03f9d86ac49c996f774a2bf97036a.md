Let me look at the Tokio `Notify` semantics and how the queue notification flows between workers more carefully.

Now I have all the information needed to make a definitive assessment. Let me trace the exact code paths.

**Key facts from the code:**

1. **Worker role assignment** (`verify_mgr.rs:185-188`): Worker 0 gets `OnlySmallCycleTx` when `worker_num > 1`.

2. **Shared `Notify`** (`verify_queue.rs:60,124`): All workers share the same `Arc<Notify>` via `subscribe()`. There is no per-role notification channel.

3. **`re_notify()` fires `notify_one()`** (`verify_queue.rs:241`): This wakes up exactly ONE waiter on the shared `Notify`.

4. **Worker 1's inner loop** (`verify_mgr.rs:110`): Worker 1 loops inside `process_inner` without returning to `tokio::select!` between txs — it never waits on `queue_ready.notified()` while the queue is non-empty.

5. **Worker 0's `pop_front(true)` path** (`verify_mgr.rs:132-144`): When all txs are large-cycle, `peek(true)` returns `None` → `re_notify()` is called → `return` exits `process_inner`.

6. **`is_large_cycle` classification** (`verify_queue.rs:212-214`): Set from the peer-declared `cycles` value, not from actual verification.

**The loop:**

```
Worker 0:
  queue_ready.notified() fires
  → process_inner()
    → is_empty() = false (read lock)
    → write lock acquired
    → pop_front(true) = None (all large-cycle)
    → re_notify() → notify_one() stored
    → return
  → tokio::select! → queue_ready.notified() fires immediately (stored notification)
  → repeat
```

While worker 1 is executing scripts (the slow part), it holds no lock and is not waiting on `notified()`. So `re_notify()`'s `notify_one()` is always consumed by worker 0.

**The write lock contention:** Worker 0's loop acquires the write lock on every iteration. Worker 1 also needs the write lock to pop the next tx. Since worker 0's loop runs orders of magnitude faster than worker 1's script execution, worker 0 can dominate write lock acquisition, starving worker 1.

**Attack path:** A remote peer submits txs via the relay protocol (`transactions_process.rs:87`) with `declared_cycles > max_tx_verify_cycles` (but ≤ `max_block_cycles` to avoid the ban at line 66). These are admitted to the verify queue with `is_large_cycle = true`.

---

### Title
OnlySmallCycleTx Worker Enters Unbounded re_notify Loop Under Large-Cycle Tx Flood, Causing Write-Lock Starvation of SubmitTimeFirst Workers — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

### Summary

When `max_tx_verify_workers > 1`, worker 0 is assigned `WorkerRole::OnlySmallCycleTx`. All workers share a single `Arc<Notify>`. If the verify queue contains only large-cycle transactions, worker 0 repeatedly wakes up, acquires the write lock, finds nothing to process, calls `re_notify()` (which fires `notify_one()` back onto the same shared `Notify`), and returns — only to be immediately re-woken. Because worker 1 is inside its own `process_inner` loop executing scripts and never waiting on `notified()`, every `re_notify()` notification is consumed by worker 0 again. The result is a tight async loop on worker 0 that generates sustained write-lock contention, degrading worker 1's ability to pop and process large-cycle transactions.

### Finding Description

**Worker role and shared notify:**

Worker 0 is assigned `OnlySmallCycleTx` when `worker_num > 1`. [1](#0-0) 

All workers obtain their wakeup handle from the same `Arc<Notify>` stored in `VerifyQueue`. [2](#0-1) [3](#0-2) 

**`re_notify()` fires `notify_one()` on the shared channel:** [4](#0-3) 

**Worker 0's `process_inner` path when queue has only large-cycle txs:**

`peek(true)` scans for `!is_large_cycle` and returns `None` when all entries are large-cycle. [5](#0-4) 

`pop_front(true)` returns `None`, the non-empty check passes, `re_notify()` is called, and the function returns. [6](#0-5) 

**Worker 1 never waits on `notified()` while the queue is non-empty:**

Worker 1 loops inside `process_inner` without returning to `tokio::select!` between transactions. [7](#0-6) 

So `re_notify()`'s `notify_one()` is always consumed by worker 0 (the only waiter), not worker 1.

**`is_large_cycle` is set from peer-declared cycles, not verified cycles:** [8](#0-7) 

A remote peer can declare any cycle value up to `max_block_cycles` without being banned. [9](#0-8) 

**The loop per iteration:**
1. `queue_ready.notified()` returns immediately (stored notification)
2. Read lock acquired for `is_empty()` check
3. Write lock acquired for `pop_front(true)`
4. `pop_front(true)` → `None`
5. `re_notify()` → stores next notification
6. Return → go to step 1

### Impact Explanation

Worker 0 generates a sustained stream of write-lock acquisitions with no useful work. Since Tokio's `RwLock` queues write-lock requests, worker 0's rapid-fire acquisitions compete directly with worker 1's write-lock requests (needed to pop each large-cycle tx). Worker 0's loop runs at async-scheduling speed (potentially thousands of iterations per second), while worker 1 processes one tx per script execution (potentially hundreds of milliseconds each). This asymmetry means worker 0 can dominate write-lock ownership, significantly reducing worker 1's throughput and degrading transaction verification for all honest users.

### Likelihood Explanation

The default `max_tx_verify_workers` is `max(num_cpus * 3/4, 1)`, so on any multi-core node the condition `worker_num > 1` holds. [10](#0-9) 

The default `max_tx_verify_cycles` is `70_000_000`. [11](#0-10) 

An attacker only needs to relay transactions with `declared_cycles` slightly above this threshold (but below `max_block_cycles`) to fill the queue with large-cycle entries. No PoW, no key, no privileged access is required — only a P2P relay connection.

### Recommendation

Replace the single shared `Notify` with per-role notification channels: one for small-cycle workers and one for large-cycle workers. When `add_tx` adds a large-cycle tx, it should notify only the large-cycle channel. `re_notify()` should target only workers capable of processing the pending tx type. Alternatively, before calling `re_notify()`, check whether any `SubmitTimeFirst` worker is actually idle (waiting), and only then fire the notification.

### Proof of Concept

```rust
// Spawn VerifyMgr with worker_num=2
// Fill queue with N txs where declared_cycles = large_cycle_threshold + 1
// Measure: worker 0 acquires write lock O(thousands/sec), worker 1 acquires O(1/processing_time)
// Observe: worker 1 throughput drops proportionally to worker 0's lock contention rate
```

Concretely: spawn a `VerifyMgr` with `worker_num=2`, enqueue 100 transactions all with `declared_cycles = max_tx_verify_cycles + 1`, instrument the write-lock acquisition count per worker over 10 seconds. Worker 0 will show orders-of-magnitude more acquisitions than worker 1, confirming the starvation. [12](#0-11)

### Citations

**File:** tx-pool/src/verify_mgr.rs (L87-87)
```rust
        let queue_ready = self.tasks.read().await.subscribe();
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

**File:** tx-pool/src/component/verify_queue.rs (L123-126)
```rust
    /// get a queue_rx to subscribe the txs count in the queue
    pub fn subscribe(&self) -> Arc<Notify> {
        Arc::clone(&self.ready_rx)
    }
```

**File:** tx-pool/src/component/verify_queue.rs (L187-191)
```rust
        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };
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

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
