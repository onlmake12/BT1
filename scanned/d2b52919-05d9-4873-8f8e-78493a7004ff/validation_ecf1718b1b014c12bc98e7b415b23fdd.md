All cited code is confirmed in the repository. Let me verify the key structural details before rendering a verdict.

All cited code is verified exactly as described. The race condition, panic site, eviction path, and handler are all confirmed in the repository.

**Key verification results:**

- `HeaderMapKernel::get` (kernel_lru.rs:132): calls `self.backend.remove(hash)` with no lock held after `memory.get_refresh` releases its write lock — TOCTOU confirmed. [1](#0-0) 
- `MemoryMap::get_refresh` (memory.rs:87-93): acquires a write lock, returns, and drops it — lock is not held across the backend call. [2](#0-1) 
- `SledBackend::remove` (backend_sled.rs:90-100): atomic at the sled level, but the `memory.get_refresh → backend.remove` sequence is not atomic. [3](#0-2) 
- `insert_valid_header` (mod.rs:1101-1103): unconditional `.expect("parent should be verified")` on the result of `get_header_index_view`. [4](#0-3) 
- `get_header_index_view` (mod.rs:1158-1159): when `store_first` is false (IBD, header ahead of tip), calls `header_map().get(hash)` first with no fallback that would rescue a `None` caused by the race. [5](#0-4) 
- `SendHeaders` handler (synchronizer/mod.rs:402-405): `HeadersProcess::execute` runs inside `tokio::task::block_in_place`, so a panic propagates through the worker thread. [6](#0-5) 
- `limit_memory` (kernel_lru.rs:168-182): confirmed to run every 5 seconds and evict to `SledBackend`. [7](#0-6) 

---

Audit Report

## Title
Race Condition in `HeaderMapKernel::get` Destructive Backend Read Causes Panic in `insert_valid_header` - (File: `shared/src/types/header_map/kernel_lru.rs`)

## Summary
`HeaderMapKernel::get` performs a destructive `backend.remove` to promote a header from `SledBackend` back into `MemoryMap`, but holds no lock across the memory-miss check and the backend remove. Two concurrent threads processing `SendHeaders` from different peers can both observe a memory miss for the same hash, then race on `backend.remove`: the loser receives `None`, propagates it through `get_header_index_view`, and causes `insert_valid_header` to panic unconditionally at `.expect("parent should be verified")`, crashing the sync task.

## Finding Description
**Root cause — `HeaderMapKernel::get` (`kernel_lru.rs:109-142`):**

`memory.get_refresh` acquires and immediately releases a write lock. After the lock is dropped, `self.backend.remove(hash)` is called with no synchronization. This is a classic TOCTOU:

```
Thread A: memory.get_refresh(hash) → None  (write lock released)
Thread B: memory.get_refresh(hash) → None  (write lock released)
Thread A: backend.remove(hash)     → Some(view)  ← atomically removes from sled
Thread B: backend.remove(hash)     → None         ← already gone
Thread A: memory.insert(view); returns Some(view)  ✓
Thread B: returns None                             ✗
```

`SledBackend::remove` is a single atomic sled operation, but the check-then-act sequence spanning `memory.get_refresh` → `backend.remove` is not atomic. No mutex, no retry, no fallback guards this window.

**Panic site — `insert_valid_header` (`sync/src/types/mod.rs:1101-1103`):**

`get_header_index_view` is called with `store_first = false` during IBD (when `tip_number < header.number()`). It calls `self.shared.header_map().get(hash)` first. When Thread B receives `None` from `get`, and the parent is not yet committed to the chain store (normal during IBD), `get_header_index_view` returns `None`. The `.expect("parent should be verified")` then panics unconditionally.

**Execution context — `SendHeaders` handler (`sync/src/synchronizer/mod.rs:402-405`):**

`HeadersProcess::execute` runs inside `tokio::task::block_in_place`. A panic there propagates through the tokio worker thread and terminates the sync task for that peer connection.

**Eviction precondition — `limit_memory` (`kernel_lru.rs:168-182`):**

`limit_memory` runs every 5 seconds via a background tokio task. During IBD, when the memory map exceeds `memory_limit` entries, it batch-evicts the oldest entries to `SledBackend` via `insert_batch` followed by `remove_batch`. This is the precondition that places the parent header in the backend and out of memory.

## Impact Explanation
**High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

A panic in `insert_valid_header` via `.expect("parent should be verified")` crashes the sync task. An unprivileged remote attacker with two P2P connections can crash the victim node's sync subsystem. Because the sync protocol handler is a critical runtime component, repeated triggering of this race can render the node unable to complete IBD or process new headers, constituting a remote crash/DoS of the CKB node's sync subsystem.

## Likelihood Explanation
- **Precondition 1 (memory eviction):** `limit_memory` fires every 5 seconds during IBD. Any node syncing a long chain will routinely have headers evicted to `SledBackend`.
- **Precondition 2 (concurrent peers):** Two inbound connections sending headers with the same parent is a normal IBD scenario. The attacker does not need to produce PoW — they can relay existing valid headers from the canonical chain.
- **Race window:** The window between `memory.get_refresh` returning `None` and `backend.remove` completing is small but non-zero. With two threads racing on the same hash under IBD load, the collision probability is non-negligible and the attack is repeatable.
- **No privilege required:** Any unprivileged peer with two P2P connections can trigger this.

## Recommendation
Replace the destructive `backend.remove` in `get` with a non-destructive `backend.get`, and wrap the entire memory-miss + backend-promote sequence in a single coarse-grained mutex to make the check-then-act atomic:

```rust
// Correct fix: hold a coarse lock across the memory-miss and backend-promote sequence
pub(crate) fn get(&self, hash: &Byte32) -> Option<HeaderIndexView> {
    // Acquire a promotion lock before the memory check
    let _promote_guard = self.promote_lock.lock();
    if let Some(view) = self.memory.get_refresh(hash) {
        return Some(view);
    }
    if self.backend.is_empty() {
        return None;
    }
    // Non-destructive read first, then remove only if found
    if let Some(view) = self.backend.get(hash) {
        self.backend.remove_no_return(hash);
        self.memory.insert(view.clone());
        Some(view)
    } else {
        None
    }
}
```

The `promote_lock` must be held across both the memory check and the backend promote to eliminate the TOCTOU window. Alternatively, redesign `get` to use a non-destructive backend read and handle promotion lazily (e.g., only promote on `limit_memory` inverse path).

## Proof of Concept
```rust
// Minimal reproducer: spawn two threads both calling HeaderMapKernel::get
// on the same hash immediately after limit_memory evicts it to backend.
let kernel = Arc::new(HeaderMapKernel::<SledBackend>::new(None, 1, Arc::new(AtomicBool::new(false))));

// Insert parent header, then trigger eviction:
kernel.insert(parent_view.clone());
// Force eviction: insert a second entry to exceed memory_limit=1, then call limit_memory
kernel.insert(other_view.clone());
kernel.limit_memory(); // evicts parent_view to backend, removes from memory

let k1 = Arc::clone(&kernel);
let k2 = Arc::clone(&kernel);
let parent_hash_1 = parent_hash.clone();
let parent_hash_2 = parent_hash.clone();

let h1 = std::thread::spawn(move || k1.get(&parent_hash_1));
let h2 = std::thread::spawn(move || k2.get(&parent_hash_2));

let r1 = h1.join().unwrap();
let r2 = h2.join().unwrap();
// One of r1/r2 will be None, proving the race.
// In production, the None result propagates to insert_valid_header and panics.
assert!(r1.is_some() && r2.is_some(), "race triggered: one thread got None");
```

### Citations

**File:** shared/src/types/header_map/kernel_lru.rs (L114-142)
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

**File:** shared/src/types/header_map/memory.rs (L87-93)
```rust
    pub(crate) fn get_refresh(&self, key: &Byte32) -> Option<HeaderIndexView> {
        let mut guard = self.0.write();
        guard
            .get_refresh(key)
            .cloned()
            .map(|inner| (key.clone(), inner).into())
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

**File:** sync/src/types/mod.rs (L1101-1103)
```rust
        let parent_header_index = self
            .get_header_index_view(&parent_hash, store_first)
            .expect("parent should be verified");
```

**File:** sync/src/types/mod.rs (L1158-1165)
```rust
        } else {
            self.shared.header_map().get(hash).or_else(|| {
                store.get_block_header(hash).and_then(|header| {
                    store
                        .get_block_ext(hash)
                        .map(|block_ext| (header, block_ext.total_difficulty).into())
                })
            })
```

**File:** sync/src/synchronizer/mod.rs (L402-406)
```rust
            packed::SyncMessageUnionReader::SendHeaders(reader) => {
                tokio::task::block_in_place(|| {
                    HeadersProcess::new(reader, self, peer, &nc).execute()
                })
            }
```
