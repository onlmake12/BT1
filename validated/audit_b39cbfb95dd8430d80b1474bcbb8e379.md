Now I have all the information needed. Let me trace the exact race condition through the code.

### Title
Concurrent `HeaderMapKernel::get` Calls Race on Destructive `backend.remove`, Causing `insert_valid_header` Panic — (`shared/src/types/header_map/kernel_lru.rs`)

---

### Summary

`HeaderMapKernel::get` promotes backend entries to memory via a destructive `backend.remove`. There is no lock or atomic guard spanning the memory-miss check and the backend removal. Two concurrent callers for the same hash both miss memory, race on `backend.remove`, and one receives `None`. The losing thread returns `None` to `insert_valid_header`, which immediately panics via `.expect("parent should be verified")`.

---

### Finding Description

`HeaderMapKernel::get` follows this sequence:

1. Acquire a write-lock on `MemoryMap` and call `get_refresh` — if the entry is absent, release the lock and continue.
2. Call `self.backend.remove(hash)` — a destructive, single-shot operation.
3. If `Some(view)` is returned, insert into memory and return `Some(view)`; otherwise return `None`. [1](#0-0) 

There is no mutex or atomic flag that prevents a second thread from executing step 1 between the first thread's step 1 and step 2. Sled's `db.remove` is per-key atomic, so exactly one caller wins the entry; the other receives `None` and returns it directly — there is no retry against memory. [2](#0-1) 

The losing thread's `None` propagates up through `get_header_index_view`: [3](#0-2) 

…and into `insert_valid_header`, which unconditionally panics: [4](#0-3) 

The same panic site is reachable from both the header-sync path (`HeaderAcceptor::accept` → `sync_shared.insert_valid_header`) and the compact-block relay path (`CompactBlockProcess::execute` → `shared.insert_valid_header`): [5](#0-4) [6](#0-5) 

---

### Impact Explanation

The panic terminates the CKB node process. An attacker who can trigger the race causes a remote crash with no local privileges required. A crashed node stops participating in consensus, block propagation, and transaction relay, constituting a denial-of-service with consensus-deviation impact.

---

### Likelihood Explanation

The preconditions are reachable in normal operation:

- **Memory limit exceeded / entries in SledBackend**: `limit_memory` runs every 5 seconds and evicts entries to sled whenever `memory.len() > memory_limit`. During IBD or rapid header sync this is routine.
- **Two concurrent callers for the same parent hash**: The sync and relay subsystems run on a Tokio thread pool. Two peers sending headers that share a common parent (e.g., two competing chain tips at the same height) will both call `insert_valid_header` concurrently. An attacker can deliberately open two connections and relay the same valid header batch from both, requiring no PoW mining — only reuse of existing valid chain headers. [7](#0-6) 

---

### Recommendation

Replace the destructive-read pattern with a non-destructive `backend.get` followed by a conditional `backend.remove_no_return` under a coarser lock, or use a `compare-and-swap`-style entry API. The simplest safe fix is to hold the `MemoryMap` write-lock across the entire promote-from-backend sequence so that only one thread performs the backend lookup and insertion while others wait and then hit the memory cache.

---

### Proof of Concept

```
Thread A                                    Thread B
─────────────────────────────────────────────────────────────────
memory.get_refresh(H)  → None
                                            memory.get_refresh(H) → None
backend.remove(H)      → Some(view)
                                            backend.remove(H)     → None  ← races here
memory.insert(view)
return Some(view)
                                            return None
                                            ↓
                                            insert_valid_header:
                                            get_header_index_view(&parent_hash)
                                            → None
                                            .expect("parent should be verified")
                                            → PANIC / process crash
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
