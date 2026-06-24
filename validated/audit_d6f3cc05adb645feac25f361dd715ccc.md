Audit Report

## Title
TOCTOU Race Between `limit_memory` and `remove` Orphans Entries in `SledBackend`, Breaking `HeaderMap` Invariants — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary
`HeaderMapKernel::limit_memory` snapshots eviction candidates via `front_n` (releasing the memory read lock), then writes them to sled via `insert_batch` with no lock held. A concurrent `remove` call can observe `backend.is_empty() == true` in the gap and skip the sled deletion, while `limit_memory` subsequently writes the same key into sled. The key is permanently orphaned in `SledBackend` with an inflated `count`, causing `get` to return stale data for explicitly-deleted headers and causing every subsequent `contains_key`/`get` to incur unnecessary sled I/O for the lifetime of the process.

## Finding Description
**Root cause:** No single lock spans the three-phase eviction sequence in `limit_memory`:

1. `self.memory.front_n(self.memory_limit)` acquires a `RwLock` read guard on `MemoryMap`, collects a `Vec` snapshot, and drops the guard on return. [1](#0-0) 
2. `self.backend.insert_batch(&values)` writes the snapshot to sled with no memory lock held. [2](#0-1) 
3. `self.memory.remove_batch(...)` acquires a write guard and removes the snapshot keys from memory. [3](#0-2) 

**`remove`** checks `backend.is_empty()` — which delegates to `self.len()`, reading the bare `AtomicUsize` `count` — after releasing the memory write lock. [4](#0-3) [5](#0-4) 

If `insert_batch` has not yet executed, `count == 0`, so `remove` returns early without calling `backend.remove_no_return`.

**`insert_batch`** accumulates a local counter and performs a single `fetch_add` only after all inserts complete, so the count is invisible to concurrent `is_empty()` checks during the entire batch write. [6](#0-5) 

**`remove_no_return`** only decrements `count` if the key was actually present in sled. [7](#0-6) 

**Exploitable interleaving for key K (in memory only):**

| Step | `limit_memory` | `remove(K)` |
|------|---------------|-------------|
| T1 | `front_n` snapshots K, lock released | — |
| T2 | — | `memory.remove(K)` → K gone from memory |
| T3 | — | `backend.is_empty()` → `count==0` → **returns early** |
| T4 | `insert_batch` writes K to sled; `count` becomes 1 | — |
| T5 | `remove_batch` tries to remove K from memory → **no-op** | — |

After T5, K exists in sled with `count = 1`. No future code path removes it. `get(K)` will find K in sled, call `backend.remove` (decrementing `count`), re-insert K into memory, and return it — returning data for a key that was explicitly deleted. [8](#0-7) 

## Impact Explanation
This is a **Medium** impact: **Suboptimal implementation of CKB state storage mechanism** (2001–10000 points).

`HeaderMap` is the authoritative in-process store for header metadata during IBD. Orphaned sled entries cause `is_empty()` to return `false` permanently (until restart), forcing every `get` and `contains_key` call to perform sled disk I/O for the remainder of the IBD session. Additionally, `get(K)` returns and re-inserts into memory a header that was explicitly removed (e.g., a discarded fork tip), breaking the `remove`-as-definitive-deletion invariant that sync logic depends on. Orphaned entries accumulate proportionally to the race frequency; during a full IBD, this can produce significant unreachable sled data and sustained disk I/O overhead on constrained nodes. [9](#0-8) 

## Likelihood Explanation
No attacker capability is required beyond sending valid headers over P2P, which is the normal IBD workload. `limit_memory` fires unconditionally every 5 seconds. [9](#0-8)  `remove` is called on every processed/discarded header. The race window is widened by `block_in_place` wrapping `insert_batch`, which can block the async executor thread for measurable time on slow storage. [10](#0-9)  Concurrent execution of `limit_memory` and `remove` is highly probable over a long IBD and requires no coordination by an attacker.

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
The first assertion will fail non-deterministically, confirming the race. The second assertion demonstrates the stale-data return path via `get`. [11](#0-10)

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

**File:** shared/src/types/header_map/kernel_lru.rs (L109-141)
```rust
    pub(crate) fn get(&self, hash: &Byte32) -> Option<HeaderIndexView> {
        #[cfg(feature = "stats")]
        {
            self.stats().tick_primary_select();
        }
        if let Some(view) = self.memory.get_refresh(hash) {
            if let Some(metrics) = ckb_metrics::handle() {
                metrics.ckb_header_map_memory_hit_miss_count.hit.inc();
            }
            return Some(view);
        }

        if let Some(metrics) = ckb_metrics::handle() {
            metrics.ckb_header_map_memory_hit_miss_count.miss.inc();
        }

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

**File:** shared/src/types/header_map/kernel_lru.rs (L161-165)
```rust
        self.memory.remove(hash, allow_shrink_to_fit);
        if self.backend.is_empty() {
            return;
        }
        self.backend.remove_no_return(hash);
```

**File:** shared/src/types/header_map/kernel_lru.rs (L172-175)
```rust
        if let Some(values) = self.memory.front_n(self.memory_limit) {
            tokio::task::block_in_place(|| {
                self.backend.insert_batch(&values);
            });
```

**File:** shared/src/types/header_map/kernel_lru.rs (L179-180)
```rust
            self.memory
                .remove_batch(values.iter().map(|value| value.hash()), allow_shrink_to_fit);
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
