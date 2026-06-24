Audit Report

## Title
Missing `remove_header_view` in Error Path of `consume_unverified_blocks` Causes Stale Sled Backend Accumulation — (File: chain/src/verify.rs)

## Summary
`ConsumeUnverifiedBlockProcessor::consume_unverified_blocks` calls `remove_header_view` in the `Ok` branch but omits it in the `Err` branch. For every contextually-invalid block that previously passed non-contextual verification (and thus had a `HeaderIndexView` inserted via `insert_valid_header`), the stale entry is never removed from the `HeaderMap`. The `HeaderMap`'s background task periodically evicts in-memory entries to an unbounded Sled backend, causing stale entries to accumulate on disk for the lifetime of the node process.

## Finding Description
In `chain/src/verify.rs` lines 141–151, the `Ok` branch calls both `remove_block_status` and `remove_header_view`:

```rust
Ok(_) => {
    self.shared.remove_block_status(&block_hash);
    self.shared.remove_header_view(&block_hash);
    ...
}
```

In lines 153–190, the `Err` branch calls `insert_block_status(block_hash, BlockStatus::BLOCK_INVALID)` (or `remove_block_status` for internal DB errors) but never calls `remove_header_view`. The stale `HeaderIndexView` remains in the `HeaderMap`.

The insertion path is confirmed: in `sync/src/relayer/compact_block_process.rs` line 78, `shared.insert_valid_header(self.peer, &header)` is called after non-contextual checks pass but before full contextual verification runs in `consume_unverified_blocks`. This means a `HeaderIndexView` is inserted for every block that clears non-contextual checks.

The `HeaderMap` in `shared/src/types/header_map/mod.rs` uses a `HeaderMapKernel<SledBackend>`. A background task fires every 5 seconds calling `limit_memory()` (`kernel_lru.rs` lines 168–182), which evicts excess entries from the bounded in-memory `MemoryMap` into the Sled backend via `backend.insert_batch`. The Sled backend (`backend_sled.rs`) has no capacity bound and no eviction policy. The `remove` function in `kernel_lru.rs` lines 153–165 correctly removes from both tiers, but it is never called in the error path.

The cleanup contract is established by `clean_expired_orphans` in `chain/src/orphan_broker.rs` lines 146–155, which calls both `remove_header_view` and `remove_block_status` for expired orphans, confirming the intended paired-cleanup pattern.

Functional correctness of `get_block_status` is not affected: `shared/src/shared.rs` lines 425–427 show it checks `block_status_map` first, and since `BLOCK_INVALID` is inserted there for failed blocks, the stale header view is never consulted for status queries.

## Impact Explanation
The impact is unbounded disk growth in the temporary Sled backend of the `HeaderMap` during the node's runtime. Each contextually-invalid block that passes non-contextual verification leaves a permanent stale `HeaderIndexView` in the Sled backend with no eviction path until node restart. This constitutes a **suboptimal implementation of CKB state storage mechanism** (Medium, 2001–10000 points). There is no functional correctness impact on block status queries, sync decisions, or consensus.

## Likelihood Explanation
Triggering this requires producing blocks with valid proof-of-work that fail contextual verification (e.g., invalid scripts, double-spends, bad DAO fields). Valid PoW is required because non-contextual/header verification includes PoW checks before `insert_valid_header` is called. This is not a low-effort operation and requires meaningful hashpower to generate many distinct valid-PoW blocks. The bug is structurally real but the practical likelihood of large-scale exploitation is low.

## Recommendation
In the `Err` branch of `consume_unverified_blocks`, add a call to `remove_header_view` to mirror the `Ok` branch and the `clean_expired_orphans` cleanup contract:

```rust
Err(err) => {
    // ... existing error handling ...
    self.shared.remove_header_view(&block_hash); // add this
}
```

## Proof of Concept
1. Connect to a CKB node as a peer with sufficient hashpower.
2. Mine a block with valid PoW and valid structure that fails contextual verification (e.g., a transaction spending a non-existent cell).
3. Send the block via the relay protocol (compact block path), triggering `insert_valid_header` at `compact_block_process.rs:78` before full verification.
4. `consume_unverified_blocks` runs: contextual verification fails, `BLOCK_INVALID` is set in `block_status_map`, but `remove_header_view` is never called (`verify.rs:153–190`).
5. The `limit_memory` background task (fires every 5 seconds, `header_map/mod.rs:62–64`) evicts the stale entry from the in-memory `MemoryMap` into the Sled backend (`kernel_lru.rs:168–182`).
6. Repeat with many distinct block hashes. The Sled backend grows without bound for the lifetime of the node process.
7. Confirm by inspecting the Sled backend size (via the `stats` feature or by observing the temp directory size) and observing it grows proportionally to the number of distinct invalid blocks submitted.