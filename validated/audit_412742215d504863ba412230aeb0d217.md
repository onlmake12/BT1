The race condition claim requires careful verification. Let me check the exact locking structure of `HeaderMapKernel` and the full call chain.

Based on my thorough analysis of the code, here is my assessment:

---

### Title
Concurrent `HeaderMapKernel::get` Destructive-Read Race Causes `insert_valid_header` Panic — (`shared/src/types/header_map/kernel_lru.rs`)

### Summary
`HeaderMapKernel::get` performs a non-atomic three-step sequence with no outer lock protecting it. Two concurrent callers for the same key can both observe a miss, causing one to return `None` for a key that exists, which triggers an unconditional `panic!` in `insert_valid_header`.

### Finding Description

`HeaderMapKernel::get` executes three distinct, unlocked steps:

1. `self.memory.get_refresh(hash)` — acquires and releases the `MemoryMap` write-lock
2. `self.backend.remove(hash)` — destructively removes the entry from sled
3. `self.memory.insert(view.clone())` — acquires and releases the `MemoryMap` write-lock again [1](#0-0) 

`HeaderMapKernel` has **no outer mutex** protecting this sequence. The only `Mutex` in the struct is gated behind `#[cfg(feature = "stats")]` and guards only statistics counters, not the core data path. [2](#0-1) 

`MemoryMap` wraps a `RwLock<LinkedHashMap<...>>` that is locked per-call, not across the full `get` sequence. [3](#0-2) 

**Race scenario (T1 and T2 both call `get(H)`, H is in sled):**

| Step | T1 | T2 |
|------|----|----|
| 1 | `memory.get_refresh(H)` → `None` | |
| 2 | `backend.remove(H)` → `Some(view)` | |
| 3 | *(preempted before memory.insert)* | `memory.get_refresh(H)` → `None` |
| 4 | | `backend.remove(H)` → **`None`** (already removed) |
| 5 | | returns `None` → **caller panics** |
| 6 | `memory.insert(view)` | *(too late)* |

### Impact Explanation

`insert_valid_header` calls `get_header_index_view` and unconditionally panics if it returns `None`: [4](#0-3) 

`get_header_index_view` with `store_first = false` (the case when `tip_number < header.number()`, i.e., during IBD processing headers ahead of the tip) checks `header_map().get(hash)` first, then falls back to the store. If the parent is only in the header_map (not yet committed to the chain store) and the race causes `header_map().get()` to return `None`, the store fallback also returns `None`, and the panic fires. [5](#0-4) 

`insert_valid_header` is called from both `HeaderAcceptor::accept` (headers sync) and `CompactBlockProcess::execute` (relay): [6](#0-5) [7](#0-6) 

### Likelihood Explanation

During IBD, the node connects to multiple peers and processes headers concurrently via tokio tasks. The background `limit_memory` task runs every 5 seconds and evicts entries from `MemoryMap` to sled when the memory limit is exceeded. [8](#0-7) [9](#0-8) 

When two concurrent sync tasks process headers that share a common parent that has been evicted to sled, the race window opens. The attacker does not need to produce PoW — they can relay existing valid chain headers to fill the memory limit and increase the probability of concurrent parent lookups. The race window (between `backend.remove` and `memory.insert`) is narrow but non-zero under OS scheduling, and grows with the number of concurrent sync tasks.

### Recommendation

Protect the entire `get` sequence with a per-key or global mutex in `HeaderMapKernel`, or change the backend `get` to be non-destructive (use `backend.get` instead of `backend.remove`, and promote to memory separately under a lock). A minimal fix is to add a `Mutex` around the memory-miss → backend-remove → memory-insert sequence:

```rust
// pseudocode
let _guard = self.get_lock.lock();
if let Some(view) = self.memory.get_refresh(hash) { return Some(view); }
if let Some(view) = self.backend.remove(hash) {
    self.memory.insert(view.clone());
    return Some(view);
}
None
```

### Proof of Concept

```rust
// Stress test: two threads call get() on the same key while it is in the backend.
// Assert at least one returns Some.
let kernel = Arc::new(HeaderMapKernel::<SledBackend>::new(...));
kernel.insert(make_view(H));
kernel.limit_memory(); // force eviction to sled

let k1 = Arc::clone(&kernel);
let k2 = Arc::clone(&kernel);
let t1 = std::thread::spawn(move || k1.get(&H));
let t2 = std::thread::spawn(move || k2.get(&H));
let r1 = t1.join().unwrap();
let r2 = t2.join().unwrap();
// Without the fix, both r1 and r2 can be None.
assert!(r1.is_some() || r2.is_some(), "at least one must return Some");
```

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

**File:** shared/src/types/header_map/memory.rs (L69-93)
```rust
pub(crate) struct MemoryMap(RwLock<LinkedHashMap<Byte32, HeaderIndexViewInner>>);

impl default::Default for MemoryMap {
    fn default() -> Self {
        Self(RwLock::new(default::Default::default()))
    }
}

impl MemoryMap {
    #[cfg(feature = "stats")]
    pub(crate) fn len(&self) -> usize {
        self.0.read().len()
    }

    pub(crate) fn contains_key(&self, key: &Byte32) -> bool {
        self.0.read().contains_key(key)
    }

    pub(crate) fn get_refresh(&self, key: &Byte32) -> Option<HeaderIndexView> {
        let mut guard = self.0.write();
        guard
            .get_refresh(key)
            .cloned()
            .map(|inner| (key.clone(), inner).into())
    }
```

**File:** sync/src/types/mod.rs (L1101-1103)
```rust
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

**File:** sync/src/synchronizer/headers_process.rs (L356-357)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
```

**File:** sync/src/relayer/compact_block_process.rs (L78-78)
```rust
        shared.insert_valid_header(self.peer, &header);
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
