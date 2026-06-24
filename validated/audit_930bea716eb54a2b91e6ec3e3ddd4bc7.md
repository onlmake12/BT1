Audit Report

## Title
`OnlySmallCycleTx` Worker Spins in Tight Async Loop on Large-Cycle-Only Queue — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

## Summary
When `max_tx_verify_workers >= 2`, Worker 0 is assigned `WorkerRole::OnlySmallCycleTx`. If the verify queue is filled exclusively with remote transactions whose declared cycle count exceeds `large_cycle_threshold`, Worker 0 enters a tight async loop: it consumes a stored Tokio `Notify` permit, finds nothing to pop, calls `re_notify()` which stores a new permit, and immediately re-wakes — never suspending. This wastes a full Tokio runtime thread and starves co-scheduled async tasks.

## Finding Description
**Root cause — single shared `Arc<Notify>` and unconditional `re_notify()`:**

`VerifyQueue` holds one `ready_rx: Arc<Notify>` shared by all workers. [1](#0-0) 

`add_tx` calls `notify_one()` on enqueue, and `re_notify()` does the same unconditionally: [2](#0-1) [3](#0-2) 

Tokio's `notify_one()` stores a permit when no waiter is present; the next `notified().await` consumes it immediately without suspending.

**Spin path — step by step:**

`Worker::run()` loops on `tokio::select!` with a `queue_ready.notified()` arm: [4](#0-3) 

`process_inner()` acquires the write lock, calls `pop_front(only_small_cycle=true)`, and on `None` with a non-empty queue calls `re_notify()` and returns: [5](#0-4) 

`peek(only_small_cycle=true)` returns `None` when all non-proposal entries have `is_large_cycle=true`: [6](#0-5) 

`is_large_cycle` is set from the remote-declared cycle count, which the attacker controls: [7](#0-6) 

**Why no yield occurs per iteration:**
1. `queue_ready.notified()` — stored permit consumed, `Poll::Ready` on first poll, no yield
2. `tasks.read().await.is_empty()` — uncontested `RwLock`, no yield
3. `tasks.write().await` — uncontested (Worker 1 holds write lock only during `pop_front`), no yield
4. `pop_front(true)` → `None`
5. `re_notify()` → new permit stored
6. Return → repeat

Tokio's `Notify::notified()` does not consume a cooperative scheduling budget unit, so Tokio's coop mechanism does not force a yield here.

**Worker 0 role assignment** is active on any multi-core host: [8](#0-7) [9](#0-8) 

## Impact Explanation
Worker 0 occupies a Tokio runtime thread without doing useful work. Tasks co-scheduled on that thread — including network I/O, sync, and RPC handlers — experience starvation. On a typical 4-core host the default produces 3 verify workers; one spinning worker wastes 33% of verify-worker parallelism and degrades overall node responsiveness. This matches **Low (501–2000 points): Any other important performance improvements for CKB**. The claim of High impact (network congestion) is not concretely proven — a single node's CPU waste does not directly cause network-wide congestion.

## Likelihood Explanation
Any P2P peer can submit transactions with attacker-controlled declared cycle counts via the standard relay path. No privilege, key, or hashpower is required. The condition `max_tx_verify_workers >= 2` is met automatically on any multi-core production host. The attacker only needs to keep the queue non-empty with large-cycle-only, non-proposal txs while Worker 1 is busy — easily maintained by continuous submission within the 256 MB queue cap. [10](#0-9) 

## Recommendation
Replace the unconditional `re_notify()` with a mechanism that cannot be consumed by the same `OnlySmallCycleTx` worker:

- **Separate `Notify` per role:** Give `SubmitTimeFirst` workers their own `Arc<Notify>` so `re_notify()` only wakes workers that can actually process large-cycle txs.
- **Explicit yield before re-notify:** Insert `tokio::task::yield_now().await` before calling `re_notify()` so Worker 0 always suspends and gives Worker 1 a chance to consume the permit first.
- **Guard on waiter presence:** Track whether any `SubmitTimeFirst` worker is currently suspended before issuing `re_notify()`, and skip it if none are waiting.

## Proof of Concept
```
1. Start a CKB node with max_tx_verify_workers = 2 (or any multi-core default).
2. Submit transactions via P2P with declared cycles > max_tx_verify_cycles
   (large_cycle_threshold), is_proposal_tx = false, so is_large_cycle = true for all.
3. Worker 1 begins processing the first large-cycle tx (slow CKB-VM execution).
4. Worker 0 wakes, calls pop_front(true) → None, calls re_notify(), returns.
5. Worker 0 immediately re-wakes (stored Notify permit), repeats.
6. Measure CPU usage of the Tokio thread running Worker 0 over 10 seconds:
   it will be near 100% while Worker 1 is processing.
7. Observe that other node tasks (sync, RPC) on the same thread experience
   increased latency during the flood.
```

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L18-18)
```rust
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L59-60)
```rust
    /// subscribe this notify to get be notified when there is item in the queue
    ready_rx: Arc<Notify>,
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

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
```

**File:** tx-pool/src/component/verify_queue.rs (L235-235)
```rust
        self.ready_rx.notify_one();
```

**File:** tx-pool/src/component/verify_queue.rs (L240-242)
```rust
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

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```
