### Title
`OnlySmallCycleTx` Worker Busy-Spin via `re_notify()` When Queue Contains Only Large-Cycle Transactions — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

---

### Summary

When `max_tx_verify_workers >= 2`, worker 0 is assigned the `OnlySmallCycleTx` role. If a remote attacker floods the verify queue with transactions whose declared cycles exceed `large_cycle_threshold`, and the sole `SubmitTimeFirst` worker is busy processing one of them, the `OnlySmallCycleTx` worker enters a tight busy-spin: `pop_front(true)` returns `None`, `re_notify()` stores a Tokio `Notify` permit via `notify_one()`, and the worker's own `queue_ready.notified()` immediately consumes that permit — re-entering `process_inner` without ever sleeping.

---

### Finding Description

**Worker role assignment** — `VerifyMgr::new` assigns `WorkerRole::OnlySmallCycleTx` to worker index 0 whenever `worker_num > 1`: [1](#0-0) 

**`is_large_cycle` is attacker-controlled** — `add_tx` derives the flag directly from the remote peer's declared cycle count: [2](#0-1) 

A remote peer can declare any cycle value; there is no pre-verification of declared cycles at queue-admission time.

**`re_notify` uses `notify_one()`** — the intent is to hand the notification to a `SubmitTimeFirst` worker, but the implementation stores a single permit on the shared `Notify`: [3](#0-2) 

**`process_inner` calls `re_notify()` and returns** — when `pop_front(true)` yields `None` but the queue is non-empty: [4](#0-3) 

**`run()` immediately re-enters `process_inner`** — the stored permit is consumed by the same worker's `notified()` call before any other waiter can claim it: [5](#0-4) 

The spin cycle is:

```
notified() resolves (permit consumed)
  → process_inner()
    → is_empty() = false
    → pop_front(true) = None
    → re_notify() → notify_one() stores permit
    → return
  → notified() resolves immediately (same permit)
  → process_inner() ...
```

Because `RwLock::read/write` are uncontended while Worker 1 holds no lock (it is inside `_process_tx`), both lock acquisitions resolve without yielding. Tokio's cooperative budget (~128 ops) forces a brief scheduler yield periodically, but the task immediately resumes — resulting in near-100 % CPU utilization on the worker's thread for the duration of the attack.

---

### Impact Explanation

The `OnlySmallCycleTx` worker monopolizes a Tokio worker thread with zero useful work. On a node with a small thread pool (the default), this degrades or stalls all other async tasks sharing that thread, including block relay, sync, and RPC handling. The effect persists as long as the queue holds at least one large-cycle entry and all `SubmitTimeFirst` workers are occupied — a condition the attacker can sustain by continuously submitting large-cycle transactions.

---

### Likelihood Explanation

- **Precondition 1**: `max_tx_verify_workers >= 2`. This is the default for any multi-core deployment.
- **Precondition 2**: All queued txs have `declared_cycles > large_cycle_threshold`. A remote peer sets this field freely; no consensus check gates queue admission.
- **Precondition 3**: At least one `SubmitTimeFirst` worker is busy. Trivially satisfied by submitting enough large-cycle txs to keep Worker 1 occupied.

No privileged access, key material, or majority hashpower is required. The attack is executable from a single P2P connection.

---

### Recommendation

Replace `notify_one()` in `re_notify()` with `notify_waiters()` so that all idle workers — including any `SubmitTimeFirst` workers — are woken:

```rust
pub fn re_notify(&self) {
    self.ready_rx.notify_waiters();
}
```

Additionally, the `OnlySmallCycleTx` worker should not call `re_notify()` at all if it is the only waiter; it should simply return and wait for the next organic notification (which arrives when a small-cycle tx is added or when a `SubmitTimeFirst` worker finishes and re-notifies). A separate `Notify` for large-cycle work, or a role-aware notification channel, would eliminate the ambiguity entirely.

---

### Proof of Concept

```
1. Configure node with max_tx_verify_workers = 2.
2. Set large_cycle_threshold = T (e.g., 3_500_000, the default max_tx_verify_cycles).
3. Submit N transactions via P2P, each with declared_cycles = T + 1.
4. Instrument re_notify() call count per second.
5. Observe: re_notify() fires at the scheduler rate (~millions/sec) while Worker 1
   is inside _process_tx for a large-cycle tx.
6. Observe: CPU core pinned at ~100% by the OnlySmallCycleTx worker task.
7. Assert: re_notify() call count is unbounded per queue-drain cycle — invariant violated.
```

### Citations

**File:** tx-pool/src/verify_mgr.rs (L98-100)
```rust
                _ = queue_ready.notified() => {
                    self.process_inner().await;
                }
```

**File:** tx-pool/src/verify_mgr.rs (L134-143)
```rust
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

**File:** tx-pool/src/component/verify_queue.rs (L212-214)
```rust
        let is_large_cycle = remote
            .map(|(cycles, _)| cycles > self.large_cycle_threshold)
            .unwrap_or(false);
```

**File:** tx-pool/src/component/verify_queue.rs (L240-242)
```rust
    pub fn re_notify(&self) {
        self.ready_rx.notify_one();
    }
```
