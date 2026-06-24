Audit Report

## Title
Concurrent Destructive-Read Race in `HeaderMapKernel::get` Causes Node Panic — (`shared/src/types/header_map/kernel_lru.rs`)

## Summary
`HeaderMapKernel::get` promotes sled-resident entries back to memory via a destructive `backend.remove`. Because no lock is held between the memory-miss check and the `backend.remove` call, two threads racing on the same evicted key will both call `remove`, and the second receives `None`. This `None` propagates to `insert_valid_header` in `sync/src/types/mod.rs`, which unconditionally panics via `.expect("parent should be verified")`, crashing the node process.

## Finding Description

**Root cause — destructive remove with no inter-thread coordination:**

`HeaderMapKernel::get` at `kernel_lru.rs` L114 calls `self.memory.get_refresh(hash)`, which internally acquires and immediately releases a `RwLock` write guard (`memory.rs` L88). After that guard is dropped, there is no synchronization protecting the subsequent `backend.remove` call at L132. [1](#0-0) 

Two threads (Thread A and Thread B) can both observe a memory miss, both pass the `backend.is_empty()` guard at L125, and both call `self.backend.remove(hash)` at L132.

**Sled `remove` is atomic per-key:**

`SledBackend::remove` calls `self.db.remove(key.as_slice())` at `backend_sled.rs` L93. Sled's per-key atomicity means Thread A receives `Some(view)` and Thread B receives `None`. Thread B therefore returns `None` from `get()`. [2](#0-1) 

**No additional synchronization at the `HeaderMap` wrapper level:**

`HeaderMap::get` at `mod.rs` L92–100 simply delegates to `self.inner.get(hash)` with no additional locking or retry logic. [3](#0-2) 

**Panic in `insert_valid_header`:**

`insert_valid_header` at `sync/src/types/mod.rs` L1096 computes `store_first = tip_number >= header.number()`. During IBD, `tip_number < header.number()`, so `store_first = false`. The `get_header_index_view` call at L1159 then checks the header map first, then falls back to the chain store. Because header H exists only in the sled backend (evicted from memory) and has not yet been committed to the chain store, both lookups return `None`. [4](#0-3) [5](#0-4) 

The unconditional `.expect("parent should be verified")` at L1103 then panics, terminating the node process.

**Concurrent callers — no serialization:**

`insert_valid_header` is called from `headers_process.rs` L356 (`HeaderAcceptor::accept`) and from `compact_block_process.rs`. Both are invoked from concurrent, unserialized P2P message-processing tasks. [6](#0-5) 

## Impact Explanation

An unprivileged remote peer can crash the CKB node process. The panic is unrecoverable (process termination). This matches the **High** impact class (10001–15000 points): *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation

During IBD the node connects to multiple peers simultaneously and processes their `SendHeaders` messages concurrently. The `limit_memory` background task runs every 5 seconds (`INTERVAL = 5000ms`) and evicts entries to sled once the memory map exceeds `WARN_THRESHOLD = size_of::<HeaderIndexView>() * 100_000`. [7](#0-6) 

Any two peers sharing a common ancestor header H that has been evicted to sled, whose child headers arrive within the same scheduling quantum, can trigger the race. No special attacker capability is required beyond connecting as a peer and sending valid headers — a normal IBD operating condition.

## Recommendation

Replace the destructive `backend.remove` in `get` with a non-destructive `backend.get`. Both racing threads will read the same value and insert it into memory (idempotent). The backend entry can then be lazily cleaned up during `limit_memory` or via a separate GC pass. Alternatively, wrap the entire memory-miss → backend-lookup → memory-insert sequence in a per-key or coarse mutex to serialize promotions.

## Proof of Concept

```rust
let map = Arc::new(HeaderMapKernel::new(None, MEMORY_LIMIT, ibd_finished));
// Insert H and evict it to sled backend
map.insert(H.clone());
map.limit_memory(); // forces eviction: memory.front_n → backend.insert_batch → memory.remove_batch

let map_a = Arc::clone(&map);
let map_b = Arc::clone(&map);

let t_a = thread::spawn(move || map_a.get(&H.hash()));
let t_b = thread::spawn(move || map_b.get(&H.hash()));

let r_a = t_a.join().unwrap();
let r_b = t_b.join().unwrap();

// One of r_a / r_b will be None due to the destructive remove race
assert!(r_a.is_some());
assert!(r_b.is_some()); // ← fails; maps directly to the production panic
```

The failing assertion corresponds exactly to the production panic at `.expect("parent should be verified")` in `insert_valid_header` (`sync/src/types/mod.rs` L1103), reachable from concurrent `SendHeaders` or compact block processing by any remote peer.

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

**File:** shared/src/types/header_map/mod.rs (L29-31)
```rust
const INTERVAL: Duration = Duration::from_millis(5000);
const ITEM_BYTES_SIZE: usize = size_of::<HeaderIndexView>();
const WARN_THRESHOLD: usize = ITEM_BYTES_SIZE * 100_000;
```

**File:** shared/src/types/header_map/mod.rs (L92-100)
```rust
    pub fn get(&self, hash: &Byte32) -> Option<HeaderIndexView> {
        let _trace_timer: Option<HistogramTimer> = ckb_metrics::handle().map(|metric| {
            metric
                .ckb_header_map_ops_duration
                .with_label_values(&["get"])
                .start_timer()
        });
        self.inner.get(hash)
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

**File:** sync/src/types/mod.rs (L1143-1166)
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
```

**File:** sync/src/synchronizer/headers_process.rs (L356-357)
```rust
        sync_shared.insert_valid_header(self.peer, self.header);
        result
```
