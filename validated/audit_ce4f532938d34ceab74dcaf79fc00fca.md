The code is confirmed. Let me trace every step against the actual implementation.

**`front_n`** (memory.rs:123-138): acquires a `read` guard, collects the snapshot into a `Vec`, and returns — the guard drops at the closing brace, releasing the lock before `limit_memory` calls `insert_batch`.

**`limit_memory`** (kernel_lru.rs:172-181): no lock is held between `front_n` returning and `insert_batch` completing.

**`remove`** (kernel_lru.rs:161-165): `memory.remove` acquires and releases a write lock, then checks `backend.is_empty()` via the bare atomic `count` — no lock on the backend.

**`insert_batch`** (backend_sled.rs:75-88): accumulates a local `count` and does a single `fetch_add` at the end — the count is not visible to concurrent readers until the entire batch is done.

**`remove_no_return`** (backend_sled.rs:102-110): only decrements `count` if the key was actually present in sled.

The interleaving described in the claim is exactly reproducible:

| Step | `limit_memory` | `remove(K)` |
|------|---------------|-------------|
| T1 | `front_n` snapshots K, lock released | — |
| T2 | — | `memory.remove(K)` → K gone from memory |
| T3 | — | `backend.is_empty()` → `count==0` → **returns early** |
| T4 | `insert_batch` writes K to sled, `count` becomes 1 | — |
| T5 | `remove_batch` tries to remove K from memory → **no-op** | — |

K is now permanently orphaned in sled with `count=1`. A subsequent `get(K)` (kernel_lru.rs:132-138) will find K in sled, remove it from sled, re-insert it into memory, and return it — returning data for a key that was explicitly deleted.

The race is real, the code path is exact, and it is reachable during ordinary IBD with no attacker capability beyond sending valid headers.

---

Audit Report

## Title
TOCTOU Race Between `limit_memory` and `remove` Orphans Entries in `SledBackend`, Breaking `HeaderMap` Invariants — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary
`HeaderMapKernel::limit_memory` snapshots eviction candidates via `front_n` (releasing the memory read lock), then writes them to sled via `insert_batch` with no lock held. A concurrent `remove` call can observe `backend.is_empty() == true` in the gap and skip the sled deletion, while `limit_memory` subsequently writes the same key into sled. The key is permanently orphaned in `SledBackend` with an inflated `count`, causing `get` to return stale data for explicitly-deleted headers and causing every subsequent `contains_key`/`get` to incur unnecessary sled I/O for the lifetime of the process.

## Finding Description
**Root cause:** No single lock spans the three-phase eviction sequence in `limit_memory`:

1. `self.memory.front_n(self.memory_limit)` — acquires a `RwLock` read guard on `MemoryMap`, collects a `Vec` snapshot, and drops the guard on return (memory.rs:123-138).
2. `self.backend.insert_batch(&values)` — writes the snapshot to sled with no memory lock held (kernel_lru.rs:173-175).
3. `self.memory.remove_batch(...)` — acquires a write guard and removes the snapshot keys from memory (kernel_lru.rs:179-180).

**`remove`** (kernel_lru.rs:161-165) checks `backend.is_empty()` — which reads the bare `AtomicUsize` `count` (backend_sled.rs:47) — after releasing the memory write lock. If `insert_batch` has not yet executed, `count == 0`, so `remove` returns early without calling `backend.remove_no_return`.

**Exploitable interleaving for key K (in memory only):**
- T1: `front_n` snapshots K, releases read lock.
- T2: `remove(K)`: `memory.remove(K)` succeeds; K is gone from memory.
- T3: `remove(K)`: `backend.is_empty()` → `count == 0` → early return; sled removal skipped.
- T4: `insert_batch` writes K to sled; `count` incremented to 1.
- T5: `remove_batch` attempts to remove K from memory → no-op (already gone).

After T5, K exists in sled with `count = 1`. No future code path removes it. `get(K)` (kernel_lru.rs:132-138) will find K in sled, call `backend.remove` (decrementing `count`), re-insert K into memory, and return it — returning data for a key that was explicitly deleted.

**Existing guards are insufficient:** `insert_batch` accumulates a local counter and performs a single `fetch_add` only after all inserts complete (backend_sled.rs:87), so the count is invisible to concurrent `is_empty()` checks during the entire batch write.

## Impact Explanation
This is a **Medium** impact: **Suboptimal implementation of CKB state storage mechanism** (2001–10000 points).

- `HeaderMap` is the authoritative in-process store for header metadata during IBD. Orphaned sled entries cause `is_empty()` to return `false` permanently (until restart), forcing every `get` and `contains_key` call to perform sled disk I/O for the remainder of the IBD session.
- `get(K)` returns and re-inserts into memory a header that was explicitly removed (e.g., a discarded fork tip), breaking the `remove`-as-definitive-deletion invariant that sync logic depends on.
- Orphaned entries accumulate proportionally to the race frequency; during a full IBD (~10M headers, `limit_memory` firing every 5 s), this can produce significant unreachable sled data and sustained disk I/O overhead on constrained nodes.

## Likelihood Explanation
No attacker capability is required beyond sending valid headers over P2P, which is the normal IBD workload. `limit_memory` fires unconditionally every 5 s (mod.rs:29). `remove` is called on every processed/discarded header. The race window is widened by `block_in_place` wrapping `insert_batch` (kernel_lru.rs:173), which can block the async executor thread for measurable time on slow storage. Concurrent execution of `limit_memory` and `remove` is highly probable over a long IBD and requires no coordination by an attacker.

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