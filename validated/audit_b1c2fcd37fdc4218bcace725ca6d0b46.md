Based on my analysis of the exact code, this is a real issue. Let me trace it precisely.

**The loop mechanics:**

`VerifyMgr::new()` assigns `WorkerRole::OnlySmallCycleTx` to worker 0 when `worker_num > 1`: [1](#0-0) 

`Worker::run()` waits on `queue_ready.notified()` and calls `process_inner()`: [2](#0-1) 

`process_inner()` calls `pop_front(true)` for `OnlySmallCycleTx`, and on `None` with a non-empty queue, calls `re_notify()` and returns: [3](#0-2) 

`re_notify()` unconditionally calls `notify_one()`: [4](#0-3) 

`peek(only_small_cycle=true)` returns `None` when all entries have `is_large_cycle=true`: [5](#0-4) 

**Tokio `Notify` semantics are the crux:** When `re_notify()` fires `notify_one()` and Worker 1 is busy (not suspended on `notified()`), the permit is *stored*. Worker 0 returns to `run()`'s `queue_ready.notified()`, which immediately consumes the stored permit without suspending. When the `RwLock` is also uncontested (Worker 1 is processing, not holding the lock), both `read().await` and `write().await` complete without yielding either. Worker 0 never suspends.

---

### Title
OnlySmallCycleTx Worker Spins in Tight Async Loop When Queue Contains Only Large-Cycle Transactions — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

### Summary
When `max_tx_verify_workers >= 2`, Worker 0 has role `OnlySmallCycleTx`. If an attacker floods the verify queue exclusively with large-cycle transactions, Worker 0 enters a tight async loop: it wakes on `queue_ready.notified()`, calls `pop_front(only_small_cycle=true)` → `None`, calls `re_notify()` which stores a Tokio `Notify` permit (because Worker 1 is busy), returns, and immediately re-wakes on the stored permit — without ever suspending. This wastes CPU and can starve other Tokio tasks sharing the same runtime thread.

### Finding Description
The `re_notify()` path in `process_inner()` is intended to hand off work to `SubmitTimeFirst` workers when `OnlySmallCycleTx` finds nothing to do. However, the implementation uses a single shared `Arc<Notify>` (`ready_rx`) for all workers. Tokio's `notify_one()` stores a permit when no waiter is present. When Worker 1 is busy processing a large-cycle tx (not suspended on `notified()`), the permit is stored and immediately consumed by Worker 0 on its next loop iteration. Since Tokio's `RwLock` also completes without yielding when uncontested, Worker 0 never reaches a genuine suspension point.

The loop per iteration:
1. `queue_ready.notified()` — consumes stored permit, **no yield**
2. `tasks.read().await.is_empty()` — uncontested read lock, **no yield**
3. `tasks.write().await` — uncontested write lock, **no yield**
4. `pop_front(true)` → `None`
5. `re_notify()` → stores new permit
6. Return → repeat

The attacker entry point is `add_tx` via `submit_remote_tx` over P2P: [6](#0-5) 

`is_large_cycle` is set to `true` when the remote-declared cycle count exceeds `large_cycle_threshold`: [7](#0-6) 

The queue size cap (256 MB) limits total payload but does not prevent the spin — the attacker only needs enough txs to keep the queue non-empty while Worker 1 is busy, and can continuously replenish. [8](#0-7) 

### Impact Explanation
Worker 0 consumes a full Tokio runtime thread without doing useful work. Other async tasks scheduled on the same thread (network I/O, sync, RPC) are starved. On a default Tokio multi-thread runtime the number of threads equals CPU cores, so one spinning worker represents a meaningful fraction of available parallelism. Sustained flooding keeps the spin alive indefinitely, degrading overall node throughput and latency.

### Likelihood Explanation
Any peer can submit transactions with attacker-controlled cycle declarations via the standard P2P relay path. No privilege, key, or hashpower is required. The default `max_tx_verify_workers` value determines whether the `OnlySmallCycleTx` role is active; if it is `>= 2` (the intended production default for performance), the condition is met automatically.

### Recommendation
Replace the unconditional `re_notify()` with a targeted wake-up that cannot be consumed by the same `OnlySmallCycleTx` worker. Options:
- Use a **separate `Notify`** (or channel) exclusively for `SubmitTimeFirst` workers, so `re_notify()` only wakes workers that can actually process large-cycle txs.
- Before calling `re_notify()`, insert an explicit `tokio::task::yield_now().await` so Worker 0 always suspends and gives Worker 1 a chance to consume the permit first.
- Track whether any `SubmitTimeFirst` worker is currently waiting before issuing `re_notify()`, and skip it if none are.

### Proof of Concept
```
1. Start a CKB node with max_tx_verify_workers = 2.
2. Submit 1000 transactions via P2P with declared cycles > max_tx_verify_cycles
   (large_cycle_threshold), so is_large_cycle = true for all.
3. Worker 1 begins processing the first large-cycle tx (slow).
4. Worker 0 wakes, calls pop_front(true) → None, calls re_notify(), returns.
5. Worker 0 immediately re-wakes (stored Notify permit), repeats.
6. Measure CPU usage of the Tokio thread running Worker 0 over 10 seconds:
   it will be near 100% while Worker 1 is processing.
7. Observe that other node tasks (sync, RPC) on the same thread experience
   increased latency during the flood.
```

### Citations

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

**File:** tx-pool/src/verify_mgr.rs (L130-144)
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
```

**File:** tx-pool/src/verify_mgr.rs (L185-189)
```rust
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
                    };
```

**File:** tx-pool/src/component/verify_queue.rs (L18-18)
```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L187-193)
```rust
        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
        } else {
            self.inner.iter_by_added_time().next()
        };

        entry.map(|e| e.inner.tx.proposal_short_id())
```

**File:** tx-pool/src/component/verify_queue.rs (L212-235)
```rust
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
```

**File:** tx-pool/src/component/verify_queue.rs (L240-242)
```rust
    pub fn re_notify(&self) {
        self.ready_rx.notify_one();
    }
```
