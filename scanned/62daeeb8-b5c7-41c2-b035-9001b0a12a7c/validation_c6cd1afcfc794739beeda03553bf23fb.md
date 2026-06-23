The code confirms this is a real, traceable busy-loop. Let me lay out the exact execution path.

---

### Title
`OnlySmallCycleTx` Worker Async Busy-Loop via Large-Cycle Tx Flooding — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

---

### Summary

When the verify queue contains only large-cycle transactions, the `OnlySmallCycleTx` worker enters a tight async loop: it wakes up, finds no small-cycle tx, calls `re_notify()` (which stores a `tokio::sync::Notify` permit), returns, and then immediately re-wakes itself by consuming that stored permit. This repeats without bound as long as the queue is non-empty with only large-cycle entries. An unprivileged remote peer can trigger this by flooding the queue with transactions whose `declared_cycles` falls in the range `(large_cycle_threshold, max_block_cycles]`.

---

### Finding Description

**Step 1 — Attacker entry point.**

A remote peer sends a `RelayTransactions` P2P message. In `TransactionsProcess::execute()`, the only guard that bans a peer is `declared_cycles > max_block_cycles`: [1](#0-0) 

`max_block_cycles` is typically `3_500_000_000`. The `large_cycle_threshold` is `max_tx_verify_cycles`, which defaults to `70_000_000`. Any tx with `declared_cycles` in `(70_000_000, 3_500_000_000]` passes the ban check and is admitted to the verify queue as `is_large_cycle = true`: [2](#0-1) 

**Step 2 — The busy-loop mechanism.**

`Worker::run()` waits on `queue_ready.notified()` (a shared `tokio::sync::Notify`): [3](#0-2) 

When woken, it calls `process_inner()`. Inside `process_inner()`, the `OnlySmallCycleTx` worker:

1. Checks `is_empty()` → `false` (large-cycle txs are present) — continues
2. Calls `pop_front(only_small_cycle=true)` → returns `None` (no small-cycle tx exists)
3. Since `!tasks.is_empty()`, calls `tasks.re_notify()` and **returns** [4](#0-3) 

`re_notify()` calls `self.ready_rx.notify_one()`: [5](#0-4) 

**Step 3 — Why this is a tight loop.**

`tokio::sync::Notify::notify_one()` stores a permit when no task is currently waiting. After `process_inner()` returns, the worker re-enters `tokio::select!` and calls `queue_ready.notified()`. Per tokio semantics, if a permit is already stored, `notified().await` returns **immediately** without suspending. The worker immediately calls `process_inner()` again, which again calls `re_notify()`, which stores another permit — and so on.

The loop per iteration involves only two async lock acquisitions (`read().await` + `write().await`) on an uncontended `RwLock`, making each iteration extremely fast.

**Step 4 — Why `SubmitTimeFirst` workers don't break the loop.**

`re_notify()` calls `notify_one()`, which wakes exactly one waiter. If all `SubmitTimeFirst` workers are busy processing large-cycle transactions (which is the expected state when the queue is flooded with them), no `SubmitTimeFirst` worker is waiting on `notified()`. The permit is stored and consumed by `OnlySmallCycleTx` itself when it returns to `run()`.

---

### Impact Explanation

The `OnlySmallCycleTx` worker thread spins in a tight async loop, consuming a disproportionate share of the tokio runtime's CPU budget. This degrades the throughput of all other async tasks sharing the same runtime, including block relay, sync, and RPC handling. The node remains operational but performance-degraded for as long as the attacker sustains the flood.

---

### Likelihood Explanation

The attack requires only a valid P2P connection and the ability to send `RelayTransactions` messages with `declared_cycles` above `max_tx_verify_cycles` but below `max_block_cycles`. No key material, privileged access, or hashpower is needed. The verify queue has a 256 MB size cap, but the attacker only needs to keep it non-empty with large-cycle txs — a modest, sustained stream suffices.

---

### Recommendation

In `process_inner()`, when `pop_front(only_small_cycle=true)` returns `None` but the queue is non-empty (all large-cycle), the worker should **yield to the scheduler** rather than immediately re-notifying. Options:

- Replace `tasks.re_notify()` + `return` with a `tokio::task::yield_now().await` before returning, so the worker suspends for at least one scheduler tick.
- Or: after calling `re_notify()`, add a small async sleep (e.g., `tokio::time::sleep(Duration::from_millis(1)).await`) before returning, preventing the tight loop.
- Or: use a separate `Notify` for large-cycle vs. small-cycle notifications so `OnlySmallCycleTx` is not woken by large-cycle-only events.

---

### Proof of Concept

```
1. Connect to a CKB node as a P2P peer (RelayV3 protocol).
2. Announce N distinct tx hashes via RelayTransactionHashes.
3. When the node sends GetRelayTransactions, respond with RelayTransactions
   where each tx has declared_cycles = large_cycle_threshold + 1
   (e.g., max_tx_verify_cycles + 1 = 70_000_001).
4. Repeat with fresh tx hashes to keep the queue non-empty.
5. Observe: the OnlySmallCycleTx worker thread (worker_id=0) consumes
   elevated CPU continuously, measurable via /proc/<pid>/task/<tid>/stat
   or tokio-console, while no small-cycle txs are present in the queue.
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
