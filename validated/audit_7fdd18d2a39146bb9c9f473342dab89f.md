Now I have all the code needed to validate each step. Let me verify the critical path through `get_block_status` as well.

Audit Report

## Title
TOCTOU Race in `HeaderMapKernel::limit_memory` Creates Ghost Sled Entries That Corrupt `get_block_status` — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary
`limit_memory` performs a non-atomic snapshot-then-write sequence: `front_n` collects keys under a read lock then releases it, `insert_batch` writes those keys to sled, and `remove_batch` removes them from memory. A concurrent `remove` call that executes between `front_n` and `insert_batch` sees an empty backend, skips the sled deletion, and removes the key from memory. `insert_batch` then writes the key to sled, and `remove_batch` silently no-ops for the already-absent key. The key becomes a permanent ghost in sled for the node's lifetime, causing `get_block_status` to return `HEADER_VALID` for headers that were explicitly removed, and `get` to resurrect stale `HeaderIndexView` data into memory.

## Finding Description

**Step 1 — `front_n` releases the lock after snapshotting:**

`front_n` acquires a read lock on the `LinkedHashMap`, collects an owned `Vec<HeaderIndexView>`, and drops the lock when it returns. [1](#0-0) 

After return, no lock protects the snapshot from concurrent mutation.

**Step 2 — Concurrent `remove` skips backend deletion:**

`remove` removes the key from memory, then checks `backend.is_empty()` as a fast-path guard. `is_empty()` delegates to `self.count.load(Ordering::SeqCst)`. [2](#0-1) [3](#0-2) 

If the backend count is still 0 (because `insert_batch` has not yet run), `remove` returns early and never calls `backend.remove_no_return`.

**Step 3 — `insert_batch` writes the ghost to sled:**

`insert_batch` runs after `front_n` released the lock, writing all snapshotted keys — including `hash_k` — to sled and incrementing `count`. [4](#0-3) [5](#0-4) 

**Step 4 — `remove_batch` silently no-ops for `hash_k`:**

`remove_batch` iterates the snapshotted keys and removes them from memory. Because `hash_k` was already removed by the concurrent `remove`, the `guard.remove(&key)` call returns `None` and no backend cleanup is performed. [6](#0-5) 

`hash_k` is now absent from memory but present in sled — a ghost entry.

**Ghost resurrection via `get`:**

A subsequent `get(hash_k)` misses memory, finds the ghost in sled via `backend.remove`, re-inserts it into memory, and returns the stale `HeaderIndexView`. [7](#0-6) 

**Concrete corruption of `get_block_status`:**

After successful block verification, `consume_unverified_blocks` removes both the block status and the header view: [8](#0-7) 

`get_block_status` then falls through to `header_map().contains_key()` when the block is absent from `block_status_map`. If a ghost entry exists in sled, `contains_key` returns `true` and the function returns `HEADER_VALID` instead of the correct `BLOCK_VALID` or `BLOCK_STORED`: [9](#0-8) 

`contains_key` in the kernel reaches the backend when memory misses and `backend.is_empty()` is false (which it is, since `insert_batch` has run): [10](#0-9) 

**Existing guards are insufficient:** The `backend.is_empty()` fast-path in `remove` is not atomic with `insert_batch`. There is no lock held across the `front_n` → `insert_batch` → `remove_batch` sequence, and `remove_batch` performs no backend cleanup for keys already absent from memory.

## Impact Explanation

This is a **Medium** finding: *Suboptimal implementation of CKB state storage mechanism* (2001–10000 points).

The `HeaderMap` is the authoritative in-process store for header index data during sync. Ghost entries corrupt two observable behaviors:

1. **`get_block_status` returns `HEADER_VALID` for verified blocks** whose header was removed. The sync state machine uses this status to gate block fetching and processing decisions (e.g., `new_block_received` rejects blocks not in `HEADER_VALID` state). Returning the wrong status for already-verified blocks corrupts the sync state machine's view of which blocks are known and at what stage.
2. **`get` resurrects stale `HeaderIndexView` data** into memory, causing callers that rely on header index data (total difficulty, skip hash, epoch) to operate on values that were intentionally evicted.

Ghost entries persist in the sled `TempDir` for the entire node lifetime and accumulate under sustained header relay, growing disk usage proportionally to the race hit rate.

## Likelihood Explanation

The race window is the interval between `front_n` returning and `insert_batch` completing (including `tokio::task::block_in_place`). `limit_memory` fires every 5 seconds via the background task. [11](#0-10) 

Block verification calls `remove_header_view` → `header_map.remove` concurrently from the `ConsumeUnverifiedBlocks` thread. An unprivileged peer relaying valid headers at a rate that keeps memory near `memory_limit` maximizes `limit_memory` invocation frequency and the probability of a concurrent `remove` landing in the window. No special privileges are required.

## Recommendation

The root cause is that `remove_batch` does not clean up backend entries for keys already absent from memory. The minimal fix is to issue an unconditional `backend.remove_no_return` for each key in `limit_memory` after `insert_batch`, regardless of whether `remove_batch` finds the key in memory:

```rust
// After insert_batch, before remove_batch:
for value in &values {
    self.backend.remove_no_return(&value.hash());
}
self.memory.remove_batch(...);
```

Alternatively, remove the `backend.is_empty()` fast-path guard in `remove` and always call `backend.remove_no_return`. This ensures any entry written to sled after a concurrent `remove` is cleaned up on the next `remove` call. A stricter fix holds the memory write lock across the entire `front_n` + `insert_batch` + `remove_batch` sequence, eliminating the race entirely.

## Proof of Concept

```
Thread A (limit_memory):                  Thread B (remove hash_k):
front_n() → snapshots {hash_k, ...}
  read lock acquired then released
                                          memory.remove(hash_k)  ← succeeds, key gone from memory
                                          backend.is_empty() → true (count==0) → return early
                                          backend.remove_no_return NOT called
backend.insert_batch([hash_k, ...])
  → sled.insert(hash_k, view)
  → count.fetch_add(1) → count==1
memory.remove_batch([hash_k, ...])
  → guard.remove(hash_k) → None (already gone)
  → no backend cleanup

State: hash_k absent from memory, present in sled (ghost, count==1)

Later — get_block_status(hash_k):
  block_status_map.get(hash_k) → None (removed by remove_block_status)
  header_map.contains_key(hash_k):
    memory.contains_key → false
    backend.is_empty() → false (count==1)
    backend.contains_key(hash_k) → true  ← ghost hit
  → returns HEADER_VALID  ← WRONG (should be BLOCK_VALID/BLOCK_STORED)

Later — get(hash_k):
  memory.get_refresh → None
  backend.is_empty() → false
  backend.remove(hash_k) → Some(stale_view)  ← ghost resurrected
  memory.insert(stale_view)
  → returns stale HeaderIndexView
```

A deterministic unit test can reproduce this by:
1. Constructing a `HeaderMapKernel` with `memory_limit = N`.
2. Inserting `N+1` headers to trigger `limit_memory`.
3. Calling `remove(hash_k)` on a thread immediately after `front_n` returns (use a `Barrier` or channel to synchronize) but before `insert_batch` completes.
4. Asserting that after `limit_memory` finishes, `contains_key(hash_k)` returns `false` — the test will fail, confirming the ghost.

### Citations

**File:** shared/src/types/header_map/memory.rs (L123-138)
```rust
    pub(crate) fn front_n(&self, size_limit: usize) -> Option<Vec<HeaderIndexView>> {
        let guard = self.0.read();
        let size = guard.len();
        if size > size_limit {
            let num = size - size_limit;
            Some(
                guard
                    .iter()
                    .take(num)
                    .map(|(key, value)| (key.clone(), value.clone()).into())
                    .collect(),
            )
        } else {
            None
        }
    }
```

**File:** shared/src/types/header_map/memory.rs (L140-156)
```rust
    pub(crate) fn remove_batch(&self, keys: impl Iterator<Item = Byte32>, shrink_to_fit: bool) {
        let mut guard = self.0.write();
        let mut keys_count = 0;
        for key in keys {
            if let Some(_old_value) = guard.remove(&key) {
                keys_count += 1;
            }
        }

        if let Some(metrics) = ckb_metrics::handle() {
            metrics.ckb_header_map_memory_count.sub(keys_count)
        }

        if shrink_to_fit {
            shrink_to_fit!(guard, SHRINK_THRESHOLD);
        }
    }
```

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

**File:** shared/src/types/header_map/kernel_lru.rs (L125-141)
```rust
        if self.backend.is_empty() {
            return None;
        }
        #[cfg(feature = "stats")]
        {
            self.stats().tick_backend_delete();
        }
        if let Some(view) = self.backend.remove(hash) {
            #[cfg(feature = "stats")]
            {
                self.stats().tick_primary_insert();
            }
            self.memory.insert(view.clone());
            Some(view)
        } else {
            None
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

**File:** shared/src/types/header_map/kernel_lru.rs (L172-175)
```rust
        if let Some(values) = self.memory.front_n(self.memory_limit) {
            tokio::task::block_in_place(|| {
                self.backend.insert_batch(&values);
            });
```

**File:** shared/src/types/header_map/backend.rs (L14-16)
```rust
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
```

**File:** shared/src/types/header_map/backend_sled.rs (L75-88)
```rust
    fn insert_batch(&self, values: &[HeaderIndexView]) {
        let mut count = 0;
        for value in values {
            let key = value.hash();
            let last_value = self
                .db
                .insert(key.as_slice(), value.to_vec())
                .expect("failed to insert item to sled");
            if last_value.is_none() {
                count += 1;
            }
        }
        self.count.fetch_add(count, Ordering::SeqCst);
    }
```

**File:** chain/src/verify.rs (L143-145)
```rust
                self.shared.remove_block_status(&block_hash);
                let log_elapsed_remove_block_status = log_now.elapsed();
                self.shared.remove_header_view(&block_hash);
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

**File:** shared/src/types/header_map/mod.rs (L57-65)
```rust
        async_handle.spawn(async move {
            let mut interval = tokio::time::interval(INTERVAL);
            interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        if let Some(map) = map_weak.upgrade() {
                            map.limit_memory();
                        } else {
```
