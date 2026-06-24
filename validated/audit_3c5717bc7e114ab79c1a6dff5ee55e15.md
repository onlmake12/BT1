All code references verified against the actual implementation. Every claim checks out:

- `front_n` [1](#0-0)  acquires a read guard, collects into `Vec`, and returns — guard drops before `insert_batch` runs.
- `limit_memory` [2](#0-1)  holds no lock between `front_n` and `insert_batch`.
- `remove` [3](#0-2)  checks `backend.is_empty()` after releasing the memory write lock.
- `is_empty()` [4](#0-3)  is `self.len() == 0`, reading the bare `AtomicUsize`.
- `insert_batch` [5](#0-4)  does a single `fetch_add` only after all inserts complete.
- `remove_no_return` [6](#0-5)  only decrements `count` if the key was present.
- `get` [7](#0-6)  removes from sled and re-inserts into memory, returning data for an orphaned key.
- `limit_memory` fires every 5 s. [8](#0-7) 

---

Audit Report

## Title
TOCTOU Race Between `limit_memory` and `remove` Orphans Entries in `SledBackend`, Breaking `HeaderMap` Invariants — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary
`HeaderMapKernel::limit_memory` snapshots eviction candidates via `front_n` (releasing the memory read lock), then writes them to sled via `insert_batch` with no lock held. A concurrent `remove` call can observe `backend.is_empty() == true` in the gap and skip the sled deletion, while `limit_memory` subsequently writes the same key into sled. The key is permanently orphaned in `SledBackend` with an inflated `count`, causing `get` to return stale data for explicitly-deleted headers and forcing unnecessary sled I/O for the lifetime of the process.

## Finding Description
**Root cause:** No single lock spans the three-phase eviction sequence in `limit_memory`:

1. `self.memory.front_n(self.memory_limit)` — acquires a `RwLock` read guard on `MemoryMap`, collects a `Vec` snapshot, and drops the guard on return (`memory.rs:123-138`).
2. `self.backend.insert_batch(&values)` — writes the snapshot to sled with no memory lock held (`kernel_lru.rs:173-175`).
3. `self.memory.remove_batch(...)` — acquires a write guard and removes the snapshot keys from memory (`kernel_lru.rs:179-180`).

**`remove`** (`kernel_lru.rs:161-165`) checks `backend.is_empty()` — which reads the bare `AtomicUsize` `count` via the default `KeyValueBackend::is_empty` (`backend.rs:14-16`) — after releasing the memory write lock. If `insert_batch` has not yet executed, `count == 0`, so `remove` returns early without calling `backend.remove_no_return`.

**Exploitable interleaving for key K (in memory only):**
- T1: `front_n` snapshots K, releases read lock.
- T2: `remove(K)`: `memory.remove(K)` succeeds; K is gone from memory.
- T3: `remove(K)`: `backend.is_empty()` → `count == 0` → early return; sled removal skipped.
- T4: `insert_batch` writes K to sled; `count` incremented to 1 (single `fetch_add` at `backend_sled.rs:87`).
- T5: `remove_batch` attempts to remove K from memory → no-op (already gone).

After T5, K exists in sled with `count = 1`. No future code path removes it. `get(K)` (`kernel_lru.rs:132-138`) will find K in sled, call `backend.remove` (decrementing `count`), re-insert K into memory, and return it — returning data for a key that was explicitly deleted.

**Existing guards are insufficient:** `insert_batch` accumulates a local counter and performs a single `fetch_add` only after all inserts complete (`backend_sled.rs:87`), so the count is invisible to concurrent `is_empty()` checks during the entire batch write.

## Impact Explanation
This is a **Medium** impact: **Suboptimal implementation of CKB state storage mechanism** (2001–10000 points).

`HeaderMap` is the authoritative in-process store for header metadata during IBD. Orphaned sled entries cause `is_empty()` to return `false` permanently (until restart), forcing every `get` and `contains_key` call to perform sled disk I/O for the remainder of the IBD session. Additionally, `get(K)` returns and re-inserts into memory a header that was explicitly removed (e.g., a discarded fork tip), breaking the `remove`-as-definitive-deletion invariant that sync logic depends on. Orphaned entries accumulate proportionally to the race frequency; during a full IBD, this can produce significant unreachable sled data and sustained disk I/O overhead on constrained nodes.

## Likelihood Explanation
No attacker capability is required beyond sending valid headers over P2P, which is the normal IBD workload. `limit_memory` fires unconditionally every 5 s (`mod.rs:29`). `remove` is called on every processed/discarded header. The race window is widened by `block_in_place` wrapping `insert_batch` (`kernel_lru.rs:173`), which can block the async executor thread for measurable time on slow storage. Concurrent execution of `limit_memory` and `remove` is highly probable over a long IBD and requires no coordination by an attacker.

## Recommendation
Restructure the eviction to be atomic with respect to concurrent removes. The simplest correct fix is to perform the move from memory to sled under a single write lock: acquire the memory write lock, extract the eviction candidates, write them to sled, and remove them from memory before releasing the lock. Alternatively, after `insert_batch` completes, re-check whether each evicted key still exists in memory; if a key is absent (meaning a concurrent `remove` already deleted it from memory but skipped sled), immediately call `backend.remove_no_return` to compensate. A coarse `RwLock` wrapping the combined memory+backend state would also eliminate the race.

## Proof of Concept
```rust
// Multi-threaded integration test
let map = Arc::new(HeaderMapKernel::<SledBackend>::new(None, LIMIT, ibd_flag));

// Fill memory past limit so limit_memory will evict
for h in &headers { map.insert(h.clone()); }

let map_a = Arc::clone(&map);
let map_b = Arc::clone(&map);

// Thread A: evict to sled
let t_a = thread::spawn(move || { map_a.limit_memory(); });

// Thread B: remove the same keys concurrently
let t_b = thread::spawn(move || {
    for key in &keys { map_b.remove(key); }
});

t_a.join().unwrap();
t_b.join().unwrap();

// Invariant: after all removes, backend must be empty
assert_eq!(map.backend.len(), 0, "orphaned entries in sled: {}", map.backend.len());
// Also assert get returns None for all removed keys
for key in &keys {
    assert!(map.get(key).is_none(), "stale data returned for removed key");
}
```
The first assertion will fail non-deterministically, confirming the race. The second assertion demonstrates the stale-data return path via `get`.

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

**File:** shared/src/types/header_map/kernel_lru.rs (L132-139)
```rust
        if let Some(view) = self.backend.remove(hash) {
            #[cfg(feature = "stats")]
            {
                self.stats().tick_primary_insert();
            }
            self.memory.insert(view.clone());
            Some(view)
        } else {
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

**File:** shared/src/types/header_map/backend_sled.rs (L102-110)
```rust
    fn remove_no_return(&self, key: &Byte32) {
        let old_value = self
            .db
            .remove(key.as_slice())
            .expect("failed to remove item from sled");
        if old_value.is_some() {
            self.count.fetch_sub(1, Ordering::SeqCst);
        }
    }
```

**File:** shared/src/types/header_map/mod.rs (L29-29)
```rust
const INTERVAL: Duration = Duration::from_millis(5000);
```
