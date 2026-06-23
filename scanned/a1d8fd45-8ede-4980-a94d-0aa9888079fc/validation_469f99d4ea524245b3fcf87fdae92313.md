Now I have all the code I need. Let me analyze the exact execution path carefully.

Based on my thorough code analysis, here is my assessment:

---

### Title
OnlySmallCycleTx Worker O(N) Busy-Wait Loop via Large-Cycle Transaction Flooding — (`tx-pool/src/component/verify_queue.rs`)

### Summary

When `max_tx_verify_workers > 1`, worker 0 is assigned `WorkerRole::OnlySmallCycleTx`. If an attacker floods the verify queue exclusively with transactions whose declared cycle count exceeds `large_cycle_threshold`, this worker enters a persistent busy-wait loop: it wakes up, performs an O(N) linear scan of the entire queue finding nothing, calls `re_notify()` which immediately re-wakes itself, and repeats — all while holding the queue's write lock during each scan iteration, blocking the `SubmitTimeFirst` workers from making progress.

### Finding Description

**Root cause 1 — O(N) scan instead of O(1) index lookup:**

`peek(only_small_cycle=true)` at line 188 uses:
```rust
self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
``` [1](#0-0) 

The `VerifyEntry` struct declares a `#[multi_index(hashed_non_unique)]` index on `is_large_cycle` (line 46–47), which would allow O(1) existence check for small-cycle entries. This index is never used in `peek()`. Instead, the code iterates the entire `added_time`-ordered index, scanning every entry until it finds one with `is_large_cycle == false`. With N large-cycle txs in the queue, this is O(N) per call. [2](#0-1) 

**Root cause 2 — Busy-wait loop via `re_notify()` self-wakeup:**

In `process_inner()`, when `pop_front(true)` returns `None` (no small-cycle tx found) but the queue is non-empty, the worker calls `tasks.re_notify()` and then `return`s: [3](#0-2) 

`re_notify()` calls `tokio::sync::Notify::notify_one()`, which stores a permit if no waiter is currently sleeping: [4](#0-3) 

Back in `run()`, the worker immediately re-enters `process_inner()` by consuming the stored permit from `queue_ready.notified()` without actually sleeping: [5](#0-4) 

When all `SubmitTimeFirst` workers are busy processing large-cycle txs (the normal case when the queue is full of them), they are not waiting on `queue_ready.notified()`. The `OnlySmallCycleTx` worker is the only waiter, so `notify_one()` always re-wakes it. This creates a tight loop.

**Root cause 3 — Write lock held during O(N) scan:**

`pop_front()` acquires the queue's write lock before calling `peek()`: [6](#0-5) 

The write lock is held for the entire duration of the O(N) scan. During this time, `SubmitTimeFirst` workers cannot acquire the write lock to pop their own entries, stalling their processing loop.

**Attacker entry point:**

The `is_large_cycle` flag is set purely from the peer-declared cycle count in the `remote` field: [7](#0-6) 

A remote peer can declare any cycle count. The tx only needs to pass `non_contextual_verify` (structural checks) before being enqueued via `resumeble_process_tx`: [8](#0-7) 

No actual large-cycle scripts are required. The attacker submits structurally valid, minimum-size txs (~60 bytes each) with declared cycles > `large_cycle_threshold`.

**Default configuration activates the vulnerable path:**

`max_tx_verify_workers` defaults to `max(num_cpus * 3/4, 1)`, which is > 1 on any multi-core machine (i.e., all production nodes): [9](#0-8) 

Worker 0 is assigned `OnlySmallCycleTx` whenever `worker_num > 1`: [10](#0-9) 

### Impact Explanation

- **CPU exhaustion**: The `OnlySmallCycleTx` worker spins in a loop performing O(N) scans. With the queue bounded at 256 MB (`DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE`), an attacker can pack ~4M minimum-size txs, making each scan iteration traverse millions of entries. [11](#0-10) 
- **Write lock starvation**: Each scan iteration holds the queue write lock, blocking `SubmitTimeFirst` workers from dequeuing and processing legitimate large-cycle txs, degrading overall tx verification throughput.
- **Small-cycle tx starvation**: The `OnlySmallCycleTx` worker — whose purpose is to ensure small-cycle txs are processed promptly — is entirely consumed by scanning, defeating its design intent.

### Likelihood Explanation

This is reachable by any unprivileged P2P peer on mainnet. The attacker only needs to relay structurally valid transactions with a declared cycle count above `max_tx_verify_cycles`. No PoW, no key material, no privileged access. The default configuration (`max_tx_verify_workers > 1` on any multi-core node) activates the vulnerable code path on all production nodes.

### Recommendation

1. **Fix the O(N) scan**: In `peek(only_small_cycle=true)`, use the existing `hashed_non_unique` index on `is_large_cycle` to perform an O(1) check for the existence of small-cycle entries before iterating. If no small-cycle entry exists, return `None` immediately without scanning.
2. **Fix the busy-wait**: When `OnlySmallCycleTx` finds no small-cycle tx, it should not call `re_notify()` unconditionally. It should only re-notify if there is at least one small-cycle tx in the queue (checkable in O(1) with the hash index). Otherwise it should sleep until a new tx is added.
3. **Rate-limit declared-large-cycle tx admission** per peer to limit queue flooding cost.

### Proof of Concept

```
1. Connect to a CKB node with max_tx_verify_workers >= 2 (default on any multi-core machine).
2. Craft N structurally valid minimum-size CKB transactions (no valid scripts needed).
3. Relay each tx via P2P with declared_cycles = max_tx_verify_cycles + 1.
4. All N txs enter the verify queue with is_large_cycle = true.
5. SubmitTimeFirst workers begin processing them (slowly, as they are large-cycle).
6. OnlySmallCycleTx worker wakes up, calls peek(true) → O(N) scan → None.
7. Worker calls re_notify(), returns, immediately re-wakes.
8. Measure: CPU time in peek() grows linearly with N; actual tx throughput drops.
9. Assert: scan cost is O(N) not O(1); write lock hold time blocks SubmitTimeFirst workers.
```

### Citations

**File:** tx-pool/src/component/verify_queue.rs (L17-18)
```rust
// 256mb for total_tx_size limit, default max_tx_pool_size is 180mb
const DEFAULT_MAX_VERIFY_QUEUE_TX_SIZE: usize = 256_000_000;
```

**File:** tx-pool/src/component/verify_queue.rs (L44-47)
```rust

    /// whether the tx is a large cycle tx
    #[multi_index(hashed_non_unique)]
    is_large_cycle: bool,
```

**File:** tx-pool/src/component/verify_queue.rs (L187-188)
```rust
        let entry = if only_small_cycle {
            self.inner.iter_by_added_time().find(|e| !e.is_large_cycle)
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

**File:** tx-pool/src/verify_mgr.rs (L185-188)
```rust
                    let role = if idx == 0 && worker_num > 1 {
                        WorkerRole::OnlySmallCycleTx
                    } else {
                        WorkerRole::SubmitTimeFirst
```

**File:** tx-pool/src/process.rs (L335-352)
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
```

**File:** util/app-config/src/configs/tx_pool.rs (L46-48)
```rust
pub fn default_max_tx_verify_workers() -> usize {
    std::cmp::max(num_cpus::get() * 3 / 4, 1)
}
```
