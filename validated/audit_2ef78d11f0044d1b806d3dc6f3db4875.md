Now I have all the code I need to evaluate this claim. Let me trace the exact race condition path.

### Title
Race in `HeaderMapKernel::get` Destructive Backend Read Causes Panic in `HeaderAcceptor::accept` — (`shared/src/types/header_map/kernel_lru.rs`)

---

### Summary

`HeaderMapKernel::get` promotes entries from the sled backend to memory using a **destructive read** (`backend.remove`). This operation is not atomic with the preceding memory-miss check. Two concurrent callers that both miss memory can race on `backend.remove` for the same key; the loser receives `None`, which propagates as `None` from `get_header_index_view`, triggering the unconditional `panic!` in `HeaderAcceptor::accept` for any header whose status is `HEADER_VALID`.

---

### Finding Description

**Root cause — non-atomic destructive read in `HeaderMapKernel::get`**

`HeaderMapKernel::get` is:

```
memory.get_refresh(hash)   // write-locks memory, then releases
  → miss
backend.is_empty()         // check
backend.remove(hash)       // destructive: removes from sled, re-inserts into memory
``` [1](#0-0) 

There is no mutex protecting the entire `get()` body. The write-lock taken by `memory.get_refresh` is released before `backend.remove` is called. Two concurrent callers that both miss memory will both proceed to `backend.remove`. Sled's `remove` is individually atomic, so exactly one caller gets `Some(view)` and the other gets `None`.

**How `limit_memory` creates the precondition**

`limit_memory` runs every 5 seconds in a background tokio task. It:
1. Snapshots the oldest entries with `front_n` (read lock, released)
2. Writes them to sled with `backend.insert_batch` (blocking)
3. Removes them from memory with `memory.remove_batch` [2](#0-1) 

After step 2 and before step 3, H is in both memory and sled. After step 3, H is **only in sled**. Any concurrent `get(H)` call that starts after step 3 will miss memory and hit the destructive-read path.

**How `accept()` panics**

When a peer re-sends header H that is already `HEADER_VALID` but not yet `BLOCK_STORED`:

```rust
let status = self.active_chain.get_block_status(&self.header.hash());
if status.contains(BlockStatus::HEADER_VALID) {
    let header_index = sync_shared
        .get_header_index_view(
            &self.header.hash(),
            status.contains(BlockStatus::BLOCK_STORED),  // false
        )
        .unwrap_or_else(|| {
            panic!(
                "header {}-{} with HEADER_VALID should exist",
                ...
            )
        })
``` [3](#0-2) 

`get_header_index_view` with `store_first=false` calls `header_map().get(hash)` first, then falls back to the chain store. If `header_map().get` returns `None` (race loser) and H is not yet committed to the chain store (not `BLOCK_STORED`), the entire call returns `None` and the `unwrap_or_else` panics. [4](#0-3) 

---

### Impact Explanation

A panic in a tokio task terminates that task; the process itself continues. The concrete effects are:
- The sync task for the racing peer is killed and the peer connection is dropped.
- The node's header sync is disrupted; repeated triggering prevents the node from completing IBD.
- The panic is unconditional — no error path, no graceful recovery.

---

### Likelihood Explanation

During IBD the header map routinely exceeds the 256 MB default memory limit, making `limit_memory` evictions frequent. Two peers concurrently sending the same batch of headers is normal behavior (the sync protocol explicitly sends the same headers to multiple peers). The race window — between `memory.remove_batch` completing and a concurrent `get` reaching `backend.remove` — is narrow but repeatable at IBD throughput rates. An attacker controlling two peers can deliberately time re-sends of a known-accepted header to maximize collision probability.

---

### Recommendation

Replace the destructive `backend.remove` in `HeaderMapKernel::get` with a non-destructive `backend.get`, and schedule a separate cleanup pass (or use a per-key mutex/`OnceCell`) to promote entries from sled to memory without racing:

```rust
// Instead of:
if let Some(view) = self.backend.remove(hash) { ... }

// Use:
if let Some(view) = self.backend.get(hash) {
    self.memory.insert(view.clone());
    // optionally schedule async removal from backend
    Some(view)
} else {
    None
}
```

The `SledBackend` already exposes a non-destructive `get` method. [5](#0-4) 

---

### Proof of Concept

```
1. Node accepts header H from peer A → H inserted into header_map, status = HEADER_VALID.
2. limit_memory fires (memory > limit):
     backend.insert_batch([H])   // H now in sled
     memory.remove_batch([H])    // H removed from memory
3. Peers B and C concurrently send H again.
   Both enter HeaderAcceptor::accept:
     get_block_status(H) → HEADER_VALID  (not BLOCK_STORED)
     get_header_index_view(H, false)
       → header_map().get(H)
         → memory.get_refresh(H) → None  (H evicted)
         → backend.remove(H):
             Peer B wins → Some(H), re-inserts into memory, returns Some
             Peer C loses → None, returns None
     Peer C: .unwrap_or_else(|| panic!("header H with HEADER_VALID should exist"))
     → PANIC
```

### Citations

**File:** shared/src/types/header_map/kernel_lru.rs (L114-141)
```rust
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
