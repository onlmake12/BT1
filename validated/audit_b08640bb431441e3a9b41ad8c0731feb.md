Audit Report

## Title
Race Condition (TOCTOU) in `HeaderMapKernel::get` Destructive Backend Promotion Causes Panic in `insert_valid_header` - (File: `shared/src/types/header_map/kernel_lru.rs`)

## Summary

`HeaderMapKernel::get` performs a non-atomic check-then-act sequence: it acquires and releases the `MemoryMap` `RwLock` via `memory.get_refresh`, then — with no lock held — calls `backend.remove`. Two concurrent threads processing `SendHeaders` from different peers can both observe a memory miss for the same hash, then race on the sled `remove`. The losing thread receives `None`, which propagates to `insert_valid_header`'s unconditional `.expect("parent should be verified")`, causing a panic that crashes the sync task.

## Finding Description

**Root cause — `HeaderMapKernel::get` (`kernel_lru.rs:109-142`):**

`MemoryMap::get_refresh` acquires a `RwLock` write guard and releases it before returning:

```rust
pub(crate) fn get_refresh(&self, key: &Byte32) -> Option<HeaderIndexView> {
    let mut guard = self.0.write();
    guard.get_refresh(key).cloned().map(|inner| (key.clone(), inner).into())
}
```

After the lock is released, `backend.remove` is called with no synchronization primitive held across the two operations:

```rust
if let Some(view) = self.backend.remove(hash) {
    self.memory.insert(view.clone());
    Some(view)
} else {
    None
}
```

The interleaving:
```
Thread A: memory.get_refresh(hash) → None  (write lock acquired + released)
Thread B: memory.get_refresh(hash) → None  (write lock acquired + released)
Thread A: backend.remove(hash)     → Some(view)   ← atomically removes from sled
Thread B: backend.remove(hash)     → None          ← already gone
Thread A: memory.insert(view); returns Some(view)
Thread B: returns None  ← BUG
```

`SledBackend::remove` is a single atomic sled operation, but the **check-then-act** sequence spanning `memory.get_refresh` → `backend.remove` is not atomic. The `HeaderMapKernel` struct contains no promotion lock or coarser mutex to serialize this sequence.

**Eviction precondition — `limit_memory` (`kernel_lru.rs:168-182`):**

`limit_memory` fires periodically via a background tokio task. During IBD, when the memory map exceeds `memory_limit` entries, it batch-evicts the oldest entries to `SledBackend`, placing parent headers in the backend where the race can occur.

**Panic site — `insert_valid_header` (`sync/src/types/mod.rs:1094-1103`):**

`store_first` is set to `false` during IBD (`tip_number < header.number()`). `get_header_index_view` is called with `store_first = false`, which calls `self.shared.header_map().get(hash)` first. If Thread B's `get` returns `None`, it falls back to the chain store. During IBD the parent header has not yet been committed to the store, so the fallback also returns `None`. The unconditional `.expect("parent should be verified")` then panics.

**Execution context — `SendHeaders` handler (`sync/src/synchronizer/mod.rs:402-405`):**

`HeadersProcess::execute` runs inside `tokio::task::block_in_place`. A panic there propagates through the tokio worker thread and terminates the sync task.

## Impact Explanation

A panic in `insert_valid_header` via `.expect("parent should be verified")` crashes the sync task. Because `HeadersProcess::execute` runs inside `tokio::task::block_in_place` on a tokio worker thread, the panic propagates through the async runtime and terminates the node's sync subsystem. This is a **remote crash / DoS** triggerable by an unprivileged attacker with two P2P connections, matching the **High** impact class: *"Vulnerabilities which could easily crash a CKB node"* (10001–15000 points).

## Likelihood Explanation

- **Precondition 1 (memory eviction):** `limit_memory` fires periodically during IBD. Any node syncing a long chain will routinely have headers evicted to `SledBackend`.
- **Precondition 2 (concurrent peers):** Two inbound connections sending headers with the same parent is a normal IBD scenario. The attacker does not need to produce PoW — they can relay existing valid headers from the canonical chain.
- **Race window:** The window between `memory.get_refresh` returning `None` and `backend.remove` completing is small but non-zero. Under load with two threads racing on the same hash, the collision probability is non-negligible and the attack is repeatable.

## Recommendation

Replace the destructive `backend.remove` in `get` with a non-destructive `backend.get`, and perform the remove-then-reinsert only when safe, or hold a coarser lock across the memory-miss and backend-access sequence. The correct fix is to wrap the entire memory-miss + backend-promote sequence in a single mutex:

```rust
// In HeaderMapKernel, add a promotion_lock: Mutex<()>
// Then in get():
let _guard = self.promotion_lock.lock();
if let Some(view) = self.memory.get_refresh(hash) {
    return Some(view);
}
if let Some(view) = self.backend.remove(hash) {
    self.memory.insert(view.clone());
    Some(view)
} else {
    None
}
```

Alternatively, use a non-destructive `backend.get` and defer eviction from the backend to a separate cleanup pass, eliminating the destructive-read pattern entirely.

## Proof of Concept

```rust
// Spawn two threads both calling HeaderMapKernel::get on the same hash
// immediately after limit_memory evicts it to backend.
let kernel = Arc::new(HeaderMapKernel::<SledBackend>::new(None, 1, ibd_finished));
// Insert parent header, then trigger eviction:
kernel.insert(parent_view.clone());
kernel.limit_memory(); // evicts to backend (memory_limit=1, size=1 triggers eviction)

let k1 = Arc::clone(&kernel);
let k2 = Arc::clone(&kernel);
let h1 = std::thread::spawn(move || k1.get(&parent_hash));
let h2 = std::thread::spawn(move || k2.get(&parent_hash));

let r1 = h1.join().unwrap();
let r2 = h2.join().unwrap();
// One of r1/r2 will be None, proving the race.
// In production: the None return reaches insert_valid_header's .expect() and panics.
assert!(r1.is_some() && r2.is_some(), "race: one thread got None");
```