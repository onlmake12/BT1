All code references check out. The race is confirmed by the actual source. Here is the validated report:

---

Audit Report

## Title
TOCTOU Race in `HeaderMapKernel::limit_memory` Causes Ghost Sled Entries and Stale Header Resurrection — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary
`limit_memory` snapshots keys under a read lock via `front_n`, releases the lock, then writes to sled via `insert_batch`, then removes from memory via `remove_batch`. A concurrent `remove` call that executes between `front_n` and `insert_batch` sees an empty backend (`is_empty() == true`), skips `backend.remove_no_return`, and removes the key from memory. `insert_batch` then writes the key to sled, and `remove_batch` finds it already gone from memory (no-op). The key is now a ghost in sled: absent from memory, present on disk. A subsequent `get` resurrects it into memory and returns the stale `HeaderIndexView`.

## Finding Description
**Root cause — three non-atomic steps in `limit_memory`:**

`kernel_lru.rs` L172–181:
```
front_n()          // acquires read lock, collects snapshot S, drops lock
insert_batch(&S)   // writes S to sled (inside block_in_place)
remove_batch(S)    // removes S from memory
``` [1](#0-0) 

`front_n` holds only a read lock for the duration of the snapshot and drops it on return. [2](#0-1) 

**Broken guard in `remove`:**

`remove` calls `memory.remove` first, then checks `backend.is_empty()` before calling `backend.remove_no_return`. `is_empty()` delegates to `self.count.load(Ordering::SeqCst)`. [3](#0-2) [4](#0-3) 

If `insert_batch` has not yet run, `count == 0`, `is_empty()` returns `true`, and `remove_no_return` is skipped entirely. The check is not atomic with `insert_batch`.

**Race interleaving:**
```
Thread A (limit_memory):                Thread B (remove hash_k):
front_n() → snapshot S, lock released
                                        memory.remove(hash_k)  ← removes from memory
                                        backend.is_empty() → true → skip remove_no_return
backend.insert_batch(S)  ← writes hash_k to sled
memory.remove_batch(S)   ← hash_k already gone, no-op

State: hash_k absent from memory, present in sled (ghost)
```

**Ghost resurrection via `get`:**

`get` misses memory, checks `backend.is_empty()` (now non-empty), calls `backend.remove(hash)`, gets the ghost, re-inserts it into memory, and returns the stale `HeaderIndexView`. [5](#0-4) 

**Sled backend uses `TempDir`** — ghost entries persist for the node's lifetime, not permanently. [6](#0-5) 

## Impact Explanation
**Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism.**

The `HeaderMap` is the node's primary state storage for header sync. Ghost entries cause:
- `contains_key` returning `true` for explicitly removed headers (e.g., orphaned fork headers), corrupting the node's view of known headers.
- `get` resurrecting stale `HeaderIndexView` data, causing the sync layer to act on headers that were intentionally evicted.
- Ghost entries accumulating in the sled `TempDir` for the node's lifetime; under sustained header flooding this can grow to hundreds of MB.

The impact is bounded to sync-layer correctness and disk growth during a single node session (cleared on restart), placing this squarely in the Medium storage-mechanism category rather than a crash or consensus deviation.

## Likelihood Explanation
The race window is the interval between `front_n` returning and `insert_batch` completing (a blocking sled write inside `tokio::task::block_in_place`). Block verification routinely calls `remove` concurrently on the same `HeaderMap`. An unprivileged peer can keep memory near `memory_limit` by relaying valid headers, causing `limit_memory` to fire every 5 seconds. [7](#0-6) 

No special privileges are required. The attacker only needs to maintain a valid P2P connection and relay headers at a rate sufficient to keep memory above `memory_limit`.

## Recommendation
Eliminate the non-atomic window by one of:
1. **Hold the write lock across `front_n` + `remove_batch`**: take the memory write lock before `front_n`, keep it held through `insert_batch` and `remove_batch`, so no concurrent `remove` can interleave.
2. **Re-check after `insert_batch`**: after `insert_batch` completes, iterate `values` and for any key no longer present in memory, immediately call `backend.remove_no_return` to clean up the ghost.
3. **Remove the `is_empty()` fast-path in `remove`**: call `backend.remove_no_return` unconditionally (the sled call is a no-op if the key is absent), eliminating the window where a concurrent `remove` skips the backend deletion.

## Proof of Concept
Minimal unit test plan:
1. Create a `HeaderMapKernel` with `memory_limit = 1`.
2. Insert two entries `A` and `B` so memory exceeds the limit.
3. Spawn Thread A to call `limit_memory` (which will snapshot `{A}`, then call `insert_batch`, then `remove_batch`).
4. Between `front_n` returning and `insert_batch` completing (use a `std::sync::Barrier` injected into `insert_batch`), call `remove(A)` from Thread B.
5. After both threads complete, assert `contains_key(A) == false` and `get(A) == None`.
6. Without the fix, `get(A)` returns `Some(stale_view)` — the ghost has been resurrected.

### Citations

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

**File:** shared/src/types/header_map/kernel_lru.rs (L172-181)
```rust
        if let Some(values) = self.memory.front_n(self.memory_limit) {
            tokio::task::block_in_place(|| {
                self.backend.insert_batch(&values);
            });

            // If IBD is not finished, don't shrink memory map
            let allow_shrink_to_fit = self.ibd_finished.load(Ordering::Acquire);
            self.memory
                .remove_batch(values.iter().map(|value| value.hash()), allow_shrink_to_fit);
        }
```

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

**File:** shared/src/types/header_map/backend.rs (L14-16)
```rust
    fn is_empty(&self) -> bool {
        self.len() == 0
    }
```

**File:** shared/src/types/header_map/backend_sled.rs (L10-14)
```rust
pub(crate) struct SledBackend {
    count: AtomicUsize,
    db: Db,
    _tmpdir: TempDir,
}
```

**File:** shared/src/types/header_map/mod.rs (L57-76)
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
                            debug!("HeaderMap inner was dropped, exiting background task");
                            break;
                        }
                    }
                    _ = stop_rx.cancelled() => {
                        info!("HeaderMap limit_memory received exit signal, exit now");
                        break
                    },
                }
            }
        });
```
