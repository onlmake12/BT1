Let me trace through the actual code to verify this claim.

Based on my full trace of the code, here is my assessment:

---

### Title
Ghost Sled Entry via `limit_memory`/`remove` Race Causes BLOCK_INVALID Block Re-classified as HEADER_VALID — (`shared/src/types/header_map/kernel_lru.rs`)

### Summary

A TOCTOU race between `HeaderMapKernel::remove` and `HeaderMapKernel::limit_memory` can leave a ghost entry for block B in the sled backend after `remove_header_view(B)` returns. A subsequent `get_block_status(B)` call then returns `HEADER_VALID` for a block that was previously marked `BLOCK_INVALID` and cleaned up, causing the sync state machine to re-attempt downloading and verifying the invalid block.

### Finding Description

**Root cause — `kernel_lru.rs` `remove`:** [1](#0-0) 

```rust
pub(crate) fn remove(&self, hash: &Byte32) {
    let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
    self.memory.remove(hash, allow_shrink_to_fit);
    if self.backend.is_empty() {
        return;          // ← early return if backend appears empty
    }
    self.backend.remove_no_return(hash);
}
```

**`limit_memory` runs concurrently (every 5 s) as a background tokio task:** [2](#0-1) 

```rust
pub(crate) fn limit_memory(&self) {
    if let Some(values) = self.memory.front_n(self.memory_limit) {
        tokio::task::block_in_place(|| {
            self.backend.insert_batch(&values);   // ← spills to sled
        });
        self.memory.remove_batch(...);
    }
}
```

**Race window:**

| Step | Thread: `limit_memory` | Thread: `remove(B)` |
|------|------------------------|---------------------|
| 1 | `memory.front_n()` — captures B in snapshot (read lock, then released) | |
| 2 | | `memory.remove(B)` — removes B from memory (write lock) |
| 3 | | `backend.is_empty()` → **true** (sled not yet written) → **returns early** |
| 4 | `backend.insert_batch(&values)` — **inserts B into sled** | |
| 5 | `memory.remove_batch(...)` — no-op, B already gone | |

After step 5: B is absent from memory, absent from `block_status_map`, but **present in sled**.

**`get_block_status` then misclassifies B:** [3](#0-2) 

```rust
pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
    match self.block_status_map().get(block_hash) {
        Some(status_ref) => *status_ref.value(),
        None => {
            if self.header_map().contains_key(block_hash) {
                BlockStatus::HEADER_VALID   // ← hit: ghost sled entry
            } else { ... }
        }
    }
}
```

`header_map.contains_key(B)` delegates to `kernel_lru.contains_key`: [4](#0-3) 

- memory miss → `backend.is_empty()` → **false** (B is still in sled) → `backend.contains_key(B)` → **true** → returns `HEADER_VALID`.

The `SledBackend.contains_key` goes directly to the sled tree, bypassing the `count` field entirely: [5](#0-4) 

### Impact Explanation

A sync peer can send a header that passes initial validation, causing `insert_valid_header` to store it. If `limit_memory` spills it to sled and the race fires during `remove_header_view`, the block's ghost sled entry survives. Every subsequent `get_block_status` call returns `HEADER_VALID`, so the sync state machine re-queues the block for download and re-verification. This repeats indefinitely, wasting sync bandwidth and CPU. Impact is **Medium** — resource exhaustion / repeated verification failures, not a consensus or key-material breach.

### Likelihood Explanation

`limit_memory` fires every 5 seconds as a background tokio task. [6](#0-5) 

The race window is the gap between `front_n` releasing its read lock and `backend.insert_batch` completing. Under IBD with a large header backlog this window is non-trivial. An attacker with many peers can increase the probability by flooding headers to keep the memory map near its limit, maximising `limit_memory` activity. No privileged access is required; only P2P `SendHeaders` messages are needed.

### Recommendation

Remove the `is_empty()` short-circuit in `remove`. The optimisation is unsafe under concurrent `limit_memory` execution. Always call `backend.remove_no_return(hash)` unconditionally:

```rust
pub(crate) fn remove(&self, hash: &Byte32) {
    let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
    self.memory.remove(hash, allow_shrink_to_fit);
    self.backend.remove_no_return(hash);   // always attempt removal
}
```

The same fix applies to the identical `is_empty()` guard in `get`: [7](#0-6) 

### Proof of Concept

State-transition invariant test (single-process, two threads):

1. Insert `HeaderIndexView` for block B into `HeaderMapKernel` (memory).
2. Spawn thread T1: call `limit_memory` (which calls `front_n`, then sleeps briefly before `insert_batch`).
3. On main thread: call `remove(B)` while T1 is between `front_n` and `insert_batch`.
4. Join T1.
5. Assert `contains_key(B)` → **false**. Without the fix it returns **true**.
6. Wrap in `Shared`: call `insert_block_status(B, BLOCK_INVALID)`, `remove_block_status(B)`, `remove_header_view(B)` (with the race injected), then assert `get_block_status(B) != HEADER_VALID`. Without the fix the assertion fails.

### Citations

**File:** shared/src/types/header_map/kernel_lru.rs (L84-107)
```rust
    pub(crate) fn contains_key(&self, hash: &Byte32) -> bool {
        #[cfg(feature = "stats")]
        {
            self.stats().tick_primary_contain();
        }
        if self.memory.contains_key(hash) {
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_header_map_memory_hit_miss_count.hit.inc()
            }
            return true;
        }
        if let Some(metrics) = ckb_metrics::handle() {
            metrics.ckb_header_map_memory_hit_miss_count.miss.inc();
        }

        if self.backend.is_empty() {
            return false;
        }
        #[cfg(feature = "stats")]
        {
            self.stats().tick_backend_contain();
        }
        self.backend.contains_key(hash)
    }
```

**File:** shared/src/types/header_map/kernel_lru.rs (L124-127)
```rust

        if self.backend.is_empty() {
            return None;
        }
```

**File:** shared/src/types/header_map/kernel_lru.rs (L153-166)
```rust
    pub(crate) fn remove(&self, hash: &Byte32) {
        #[cfg(feature = "stats")]
        {
            self.trace();
            self.stats().tick_primary_delete();
        }
        // If IBD is not finished, don't shrink memory map
        let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
        self.memory.remove(hash, allow_shrink_to_fit);
        if self.backend.is_empty() {
            return;
        }
        self.backend.remove_no_return(hash);
    }
```

**File:** shared/src/types/header_map/kernel_lru.rs (L168-182)
```rust
    pub(crate) fn limit_memory(&self) {
        let _trace_timer: Option<HistogramTimer> = ckb_metrics::handle()
            .map(|handle| handle.ckb_header_map_limit_memory_duration.start_timer());

        if let Some(values) = self.memory.front_n(self.memory_limit) {
            tokio::task::block_in_place(|| {
                self.backend.insert_batch(&values);
            });

            // If IBD is not finished, don't shrink memory map
            let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
            self.memory
                .remove_batch(values.iter().map(|value| value.hash()), allow_shrink_to_fit);
        }
    }
```

**File:** shared/src/shared.rs (L425-444)
```rust
    pub fn get_block_status(&self, block_hash: &Byte32) -> BlockStatus {
        match self.block_status_map().get(block_hash) {
            Some(status_ref) => *status_ref.value(),
            None => {
                if self.header_map().contains_key(block_hash) {
                    BlockStatus::HEADER_VALID
                } else {
                    let verified = self
                        .snapshot()
                        .get_block_ext(block_hash)
                        .map(|block_ext| block_ext.verified);
                    match verified {
                        None => BlockStatus::UNKNOWN,
                        Some(None) => BlockStatus::BLOCK_STORED,
                        Some(Some(true)) => BlockStatus::BLOCK_VALID,
                        Some(Some(false)) => BlockStatus::BLOCK_INVALID,
                    }
                }
            }
        }
```

**File:** shared/src/types/header_map/backend_sled.rs (L50-54)
```rust
    fn contains_key(&self, key: &Byte32) -> bool {
        self.db
            .contains_key(key.as_slice())
            .expect("sled contains_key")
    }
```

**File:** shared/src/types/header_map/mod.rs (L29-29)
```rust
const INTERVAL: Duration = Duration::from_millis(5000);
```
