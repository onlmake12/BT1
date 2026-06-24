Audit Report

## Title
`OnlySmallCycleTx` Worker Async Busy-Loop via Large-Cycle Transaction Flooding — (`tx-pool/src/verify_mgr.rs`, `tx-pool/src/component/verify_queue.rs`)

## Summary

When the verify queue contains only large-cycle transactions, the `OnlySmallCycleTx` worker enters a tight async loop: it wakes, finds no small-cycle tx, calls `re_notify()` (which stores a `tokio::sync::Notify` permit), returns to `run()`, and immediately re-wakes itself by consuming that stored permit. This repeats without bound as long as the queue is non-empty with only large-cycle entries. An unprivileged remote peer can trigger this by flooding the queue with transactions whose `declared_cycles` falls in `(max_tx_verify_cycles, max_block_cycles]`.

## Finding Description

**Attacker entry point.** In `TransactionsProcess::execute()`, the only peer-banning guard is `declared_cycles > max_block_cycles`: [1](#0-0) 

Any tx with `declared_cycles` in `(large_cycle_threshold, max_block_cycles]` (i.e., `(70_000_000, 3_500_000_000]`) passes the ban check. In `add_tx()`, such a tx is classified `is_large_cycle = true`: [2](#0-1) 

**Busy-loop mechanism.** `Worker::run()` loops on `tokio::select!`, waiting on `queue_ready.notified()`: [3](#0-2) 

When woken, it calls `process_inner()`. Inside `process_inner()`, the `OnlySmallCycleTx` worker (worker index 0):
1. Calls `self.tasks.read().await.is_empty()` → `false` (large-cycle txs present)
2. Calls `tasks.pop_front(only_small_cycle=true)` → `None` (no small-cycle tx exists)
3. Since `!tasks.is_empty()`, calls `tasks.re_notify()` and **returns** from `process_inner()` [4](#0-3) 

`re_notify()` calls `self.ready_rx.notify_one()`: [5](#0-4) 

**Why this is a tight loop.** Per tokio semantics, `Notify::notify_one()` stores a permit when no task is currently waiting. After `process_inner()` returns, the worker re-enters `tokio::select!` and calls `queue_ready.notified()`. Because a permit is already stored, `notified().await` returns **immediately** without suspending. The worker calls `process_inner()` again, which again calls `re_notify()`, storing another permit — and so on indefinitely.

Each iteration involves only two async lock acquisitions (`read().await` + `write().await`) on an uncontended `RwLock`, both of which complete without yielding when uncontended. Tokio's cooperative budget (128 ops) provides periodic yields, but the worker immediately re-enters the loop after each yield, consuming a disproportionate share of the runtime's CPU budget.

**Why `SubmitTimeFirst` workers don't break the loop.** `notify_one()` wakes exactly one waiter. When all `SubmitTimeFirst` workers are busy processing large-cycle transactions (the expected state during a flood), none are waiting on `notified()`. The permit is stored and consumed exclusively by `OnlySmallCycleTx` when it returns to `run()`.

**Existing guards are insufficient.** The 256 MB queue size cap (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`) only limits total bytes, not the busy-loop behavior. The attacker only needs to keep the queue non-empty with large-cycle txs — a modest sustained stream suffices. [6](#0-5) 

## Impact Explanation

The `OnlySmallCycleTx` worker spins in a tight async loop, consuming a disproportionate share of the tokio runtime's CPU budget. This degrades the throughput of all other async tasks sharing the same runtime, including block relay, sync, and RPC handling. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.** The attacker sustains the degradation with a low-cost, continuous stream of large-cycle transactions, requiring no hashpower or privileged access.

## Likelihood Explanation

The attack requires only a valid P2P connection and the ability to send `RelayTransactions` messages. The attacker announces tx hashes via `RelayTransactionHashes`, waits for the node to issue `GetRelayTransactions`, then responds with transactions setting `declared_cycles = max_tx_verify_cycles + 1` (e.g., `70_000_001`). This passes all existing guards. The attack is repeatable with fresh tx hashes to keep the queue non-empty. No key material, privilege escalation, or hashpower is required.

## Recommendation

In `process_inner()`, when `pop_front(only_small_cycle=true)` returns `None` but the queue is non-empty (all large-cycle), the worker must yield to the scheduler rather than immediately re-notifying. Concrete options:

- **Preferred:** After calling `tasks.re_notify()`, add `tokio::task::yield_now().await` before returning, ensuring the worker suspends for at least one scheduler tick and allows other tasks to run.
- **Alternative:** Replace the immediate `re_notify()` + `return` with a small async sleep (e.g., `tokio::time::sleep(Duration::from_millis(1)).await`) to rate-limit the loop.
- **Structural fix:** Use a separate `Notify` for large-cycle vs. small-cycle notifications so `OnlySmallCycleTx` is not woken by large-cycle-only events at all.

## Proof of Concept

```
1. Connect to a CKB node as a P2P peer (RelayV3 protocol).
2. Announce N distinct tx hashes via RelayTransactionHashes.
3. When the node sends GetRelayTransactions, respond with RelayTransactions
   where each tx has declared_cycles = max_tx_verify_cycles + 1
   (e.g., 70_000_001), which is below max_block_cycles (3_500_000_000).
4. Repeat with fresh tx hashes to keep the queue non-empty.
5. Observe: the OnlySmallCycleTx worker (worker_id=0) consumes elevated CPU
   continuously, measurable via /proc/<pid>/task/<tid>/stat or tokio-console,
   while no small-cycle txs are present in the queue.
6. Confirm: block relay latency and RPC response times increase proportionally
   to the sustained flood rate.
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

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
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
