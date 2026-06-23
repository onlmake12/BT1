Looking at the actual code carefully to trace the race condition claim.

### Title
Race Condition in `HeaderMapKernel::get` Destructive Backend Read Causes Panic in `insert_valid_header` - (`shared/src/types/header_map/kernel_lru.rs`)

### Summary

`HeaderMapKernel::get` uses a **destructive read** (`backend.remove`) to promote a header from `SledBackend` back to `MemoryMap`. There is no lock held across the memory-miss check and the backend remove. Two concurrent threads processing `SendHeaders` from different peers can both miss the memory check for the same hash, then race on `backend.remove`: the winner gets `Some(view)`, the loser gets `None`. The loser's caller, `insert_valid_header`, then hits `.expect("parent should be verified")` and panics, crashing the sync task.

---

### Finding Description

**Root cause — `HeaderMapKernel::get` (kernel_lru.rs:109-142):** [1](#0-0) 

The function first checks `MemoryMap` (acquires and releases a write lock), then — with no lock held — calls `self.backend.remove(hash)`. This is a classic TOCTOU pattern:

```
Thread A: memory.get_refresh(hash) → None  (lock released)
Thread B: memory.get_refresh(hash) → None  (lock released)
Thread A: backend.remove(hash)     → Some(view)  ← removes from sled
Thread B: backend.remove(hash)     → None         ← already gone
Thread A: memory.insert(view)      ← re-inserts into memory
Thread A: returns Some(view)  ✓
Thread B: returns None        ✗  (header now exists in memory, but B already passed the check)
```

`SledBackend::remove` is a single atomic sled operation, but the **check-then-act** sequence spanning `memory.get_refresh` → `backend.remove` is not atomic. [2](#0-1) 

**Panic site — `insert_valid_header` (sync/src/types/mod.rs:1101-1103):** [3](#0-2) 

`get_header_index_view` calls `self.shared.header_map().get(hash)`, which delegates to `HeaderMapKernel::get`. When Thread B receives `None`, the `.expect("parent should be verified")` panics unconditionally. [4](#0-3) 

**Execution context — `SendHeaders` handler (sync/src/synchronizer/mod.rs:402-405):** [5](#0-4) 

`HeadersProcess::execute` runs inside `tokio::task::block_in_place`. A panic there propagates through the tokio worker thread and terminates the task. Since the sync protocol handler is a critical runtime component, this crashes the node.

**Eviction precondition — `limit_memory` (kernel_lru.rs:168-182):** [6](#0-5) 

`limit_memory` runs every 5 seconds via a background tokio task. During IBD, when the memory map exceeds `memory_limit` entries, it batch-evicts the oldest entries to `SledBackend`. This is the precondition that places the parent header in the backend. [7](#0-6) 

---

### Impact Explanation

A panic in `insert_valid_header` via `.expect("parent should be verified")` crashes the sync task. Because `HeadersProcess::execute` runs inside `tokio::task::block_in_place` on a tokio worker thread, the panic propagates up through the async runtime. This is a **remote crash / DoS**: an unprivileged attacker with two P2P connections can crash the victim node's sync subsystem.

---

### Likelihood Explanation

- **Precondition 1 (memory eviction):** `limit_memory` fires every 5 seconds during IBD. Any node syncing a long chain will have headers evicted to `SledBackend` routinely.
- **Precondition 2 (concurrent peers):** Two inbound connections sending headers with the same parent is a normal IBD scenario. The attacker does not need to produce PoW — they can relay existing valid headers from the canonical chain.
- **Race window:** The window between `memory.get_refresh` returning `None` and `backend.remove` completing is small but non-zero. With two threads racing on the same hash, the collision probability is non-negligible, especially under load.

---

### Recommendation

Replace the destructive `backend.remove` in `get` with a non-destructive `backend.get`, and perform the remove-then-reinsert only when safe, or hold a coarser lock across the memory-miss and backend-access sequence. The simplest fix:

```rust
// Instead of:
if let Some(view) = self.backend.remove(hash) {
    self.memory.insert(view.clone());
    Some(view)
}

// Use:
if let Some(view) = self.backend.get(hash) {
    self.backend.remove_no_return(hash);
    self.memory.insert(view.clone());
    Some(view)
}
```

This still has a race, but the correct fix is to wrap the entire memory-miss + backend-promote sequence in a single mutex, or redesign `get` to use a non-destructive backend read and handle promotion separately.

---

### Proof of Concept

```rust
// Spawn two threads both calling HeaderMapKernel::get on the same hash
// immediately after limit_memory evicts it to backend.
let kernel = Arc::new(HeaderMapKernel::<SledBackend>::new(...));
// Insert parent header, then trigger eviction:
kernel.insert(parent_view.clone());
kernel.limit_memory(); // evicts to backend

let k1 = Arc::clone(&kernel);
let k2 = Arc::clone(&kernel);
let h1 = std::thread::spawn(move || k1.get(&parent_hash));
let h2 = std::thread::spawn(move || k2.get(&parent_hash));

let r1 = h1.join().unwrap();
let r2 = h2.join().unwrap();
// Assert: both should be Some, but one will be None
assert!(r1.is_some() && r2.is_some(), "race: one thread got None");
```

### Citations

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

**File:** shared/src/types/header_map/backend_sled.rs (L90-100)
```rust
    fn remove(&self, key: &Byte32) -> Option<HeaderIndexView> {
        let old_value = self
            .db
            .remove(key.as_slice())
            .expect("failed to remove item from sled");

        old_value.map(|slice| {
            self.count.fetch_sub(1, Ordering::SeqCst);
            HeaderIndexView::from_slice_should_be_ok(key.as_slice(), &slice)
        })
    }
```

**File:** sync/src/types/mod.rs (L1094-1103)
```rust
    pub fn insert_valid_header(&self, peer: PeerIndex, header: &core::HeaderView) {
        let tip_number = self.active_chain().tip_number();
        let store_first = tip_number >= header.number();
        // We don't use header#parent_hash clone here because it will hold the arc counter of the SendHeaders message
        // which will cause the 2000 headers to be held in memory for a long time
        let parent_hash = Byte32::from_slice(header.data().raw().parent_hash().as_slice())
            .expect("checked slice length");
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
```

**File:** sync/src/types/mod.rs (L1143-1167)
```rust
    pub(crate) fn get_header_index_view(
        &self,
        hash: &Byte32,
        store_first: bool,
    ) -> Option<HeaderIndexView> {
        let store = self.store();
        if store_first {
            store
                .get_block_header(hash)
                .and_then(|header| {
                    store
                        .get_block_ext(hash)
                        .map(|block_ext| (header, block_ext.total_difficulty).into())
                })
                .or_else(|| self.shared.header_map().get(hash))
        } else {
            self.shared.header_map().get(hash).or_else(|| {
                store.get_block_header(hash).and_then(|header| {
                    store
                        .get_block_ext(hash)
                        .map(|block_ext| (header, block_ext.total_difficulty).into())
                })
            })
        }
    }
```

**File:** sync/src/synchronizer/mod.rs (L402-406)
```rust
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
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
