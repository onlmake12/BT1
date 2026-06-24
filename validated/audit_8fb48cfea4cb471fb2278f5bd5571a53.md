Audit Report

## Title
Non-Atomic Destructive Read Race in `HeaderMapKernel::get` Causes Unconditional Panic in `HeaderAcceptor::accept` — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary

`HeaderMapKernel::get` promotes entries from the sled backend to memory using a destructive `backend.remove` call that is not atomically guarded with the preceding memory-miss check. Two concurrent callers that both miss the memory cache will both invoke `backend.remove` for the same key; the losing caller receives `None`, which propagates through `get_header_index_view` and triggers the unconditional `panic!` in `HeaderAcceptor::accept` for any header whose status is `HEADER_VALID` but not yet `BLOCK_STORED`. An attacker controlling two peers can deliberately trigger this during IBD to repeatedly kill sync tasks and prevent the node from completing initial block download.

## Finding Description

**Root cause — unguarded destructive read in `HeaderMapKernel::get`**

`HeaderMapKernel` holds `memory: MemoryMap` (internally a `RwLock<LinkedHashMap>`) and `backend: SledBackend` as two independent fields with no outer mutex. [1](#0-0) 

The `get` method takes `&self` and executes in two separate, unguarded steps:
1. `memory.get_refresh(hash)` — acquires and **releases** the memory write-lock
2. `backend.remove(hash)` — a separate sled operation with no connection to step 1 [2](#0-1) 

Two concurrent callers that both observe a memory miss will both proceed to `backend.remove`. Sled's `remove` is individually atomic, so exactly one caller gets `Some(view)` and the other gets `None`.

**How `limit_memory` creates the precondition**

`limit_memory` runs every 5 seconds in a background tokio task. It snapshots entries with `front_n` (read lock, released), writes them to sled with `backend.insert_batch`, then removes them from memory with `memory.remove_batch`. After `remove_batch` completes, header H exists **only in sled**. [3](#0-2) 

Any concurrent `get(H)` that starts after `remove_batch` will miss memory and enter the destructive-read path.

**How `accept()` panics**

When a peer re-sends header H that is already `HEADER_VALID` but not yet `BLOCK_STORED`, `HeaderAcceptor::accept` calls `get_header_index_view` with `store_first=false` and wraps the result in an unconditional `panic!`: [4](#0-3) 

`get_header_index_view` with `store_first=false` calls `header_map().get(hash)` first, then falls back to the chain store: [5](#0-4) 

If `header_map().get` returns `None` (race loser) and H is not yet committed to the chain store (not `BLOCK_STORED`), the entire call returns `None` and the `unwrap_or_else` panics unconditionally.

**Why existing checks are insufficient**

`MemoryMap.get_refresh` holds a write-lock only for its own duration; it does not extend protection to the subsequent `backend.remove` call. There is no per-key mutex, `OnceCell`, or compare-and-swap protecting the promote-from-backend path. The `SledBackend` exposes a non-destructive `get` method that would avoid the race entirely but is not used here. [6](#0-5) 

## Impact Explanation

A panic in the tokio task processing the racing peer's sync message terminates that task and drops the peer connection. The node process itself continues, but the sync task for that peer is killed. An attacker controlling two peers can repeatedly time re-sends of a known-accepted header to continuously trigger the panic, preventing the node from completing IBD and making it effectively unable to sync — matching **High: Vulnerabilities which could easily crash a CKB node** (the node's primary sync function is rendered non-operational).

## Likelihood Explanation

During IBD the header map routinely exceeds the 256 MB default memory limit, making `limit_memory` evictions frequent (every 5 seconds). The CKB sync protocol explicitly sends the same headers to multiple peers, so two peers concurrently sending the same batch of headers is normal behavior, not an attacker requirement. The race window — between `memory.remove_batch` completing and a concurrent `get` reaching `backend.remove` — is narrow but repeatable at IBD throughput rates. An attacker controlling two peers can deliberately time re-sends of a known-accepted header (any header with `HEADER_VALID` and not `BLOCK_STORED`) to maximize collision probability with no special privileges required.

## Recommendation

Replace the destructive `backend.remove` in `HeaderMapKernel::get` with the already-available non-destructive `backend.get`, and schedule a separate cleanup pass to remove the entry from the backend after it has been safely promoted to memory:

```rust
// Instead of:
if let Some(view) = self.backend.remove(hash) {
    self.memory.insert(view.clone());
    Some(view)
} else {
    None
}

// Use:
if let Some(view) = self.backend.get(hash) {
    self.memory.insert(view.clone());
    // schedule async or deferred removal from backend
    Some(view)
} else {
    None
}
```

`SledBackend::get` is already implemented and available via the `KeyValueBackend` trait. [7](#0-6) 

## Proof of Concept

```
1. Node accepts header H from peer A → H inserted into header_map, status = HEADER_VALID.
2. limit_memory fires (memory > limit):
     backend.insert_batch([H])   // H now in sled AND memory
     memory.remove_batch([H])    // H removed from memory; H is now ONLY in sled
3. Peers B and C concurrently send H again.
   Both enter HeaderAcceptor::accept:
     get_block_status(H) → HEADER_VALID (not BLOCK_STORED)
     get_header_index_view(H, store_first=false)
       → header_map().get(H)
         → memory.get_refresh(H) → None  (H evicted)
         → backend.remove(H):
             Peer B wins → Some(H), re-inserts into memory, returns Some
             Peer C loses → None, returns None
       → chain store fallback → None (H not BLOCK_STORED)
     Peer C: .unwrap_or_else(|| panic!("header H with HEADER_VALID should exist"))
     → PANIC: sync task for peer C is killed
4. Repeat step 3 continuously to prevent IBD completion.
```

A targeted integration test can reproduce this by spawning two threads that both call `header_map.get(H)` immediately after a `limit_memory` call evicts H, asserting that neither returns `None`.

### Citations

**File:** shared/src/types/header_map/kernel_lru.rs (L16-29)
```rust
pub(crate) struct HeaderMapKernel<Backend>
where
    Backend: KeyValueBackend,
{
    pub(crate) memory: MemoryMap,
    pub(crate) backend: Backend,
    // Configuration
    memory_limit: usize,
    // if ckb is in IBD mode, don't shrink memory map
    ibd_finished: Arc<AtomicBool>,
    // Statistics
    #[cfg(feature = "stats")]
    stats: Mutex<HeaderMapKernelStats>,
}
```

**File:** shared/src/types/header_map/kernel_lru.rs (L109-142)
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
    }
```

**File:** shared/src/types/header_map/kernel_lru.rs (L168-181)
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
```

**File:** sync/src/synchronizer/headers_process.rs (L303-316)
```rust
        let status = self.active_chain.get_block_status(&self.header.hash());
        if status.contains(BlockStatus::HEADER_VALID) {
            let header_index = sync_shared
                .get_header_index_view(
                    &self.header.hash(),
                    status.contains(BlockStatus::BLOCK_STORED),
                )
                .unwrap_or_else(|| {
                    panic!(
                        "header {}-{} with HEADER_VALID should exist",
                        self.header.number(),
                        self.header.hash()
                    )
                })
```

**File:** sync/src/types/mod.rs (L1158-1166)
```rust
        } else {
            self.shared.header_map().get(hash).or_else(|| {
                store.get_block_header(hash).and_then(|header| {
                    store
                        .get_block_ext(hash)
                        .map(|block_ext| (header, block_ext.total_difficulty).into())
                })
            })
        }
```

**File:** shared/src/types/header_map/backend_sled.rs (L56-61)
```rust
    fn get(&self, key: &Byte32) -> Option<HeaderIndexView> {
        self.db
            .get(key.as_slice())
            .unwrap_or_else(|err| panic!("read header map from disk should be ok, but {err}"))
            .map(|slice| HeaderIndexView::from_slice_should_be_ok(key.as_slice(), slice.as_ref()))
    }
```

**File:** shared/src/types/header_map/backend.rs (L19-19)
```rust
    fn get(&self, key: &Byte32) -> Option<HeaderIndexView>;
```
