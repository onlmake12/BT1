Audit Report

## Title
Unbounded O(n) Full-Pool Scan in `get_max_update_time()` on Every `tx_pool_info` RPC Call — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::get_max_update_time()` performs a full linear iteration over all pool entries to compute the maximum timestamp on every invocation. Unlike every other pool-wide aggregate (`total_tx_size`, `total_tx_cycles`, `pending_count`, etc.), this value is never cached. It is called unconditionally from `TxPoolService::info()`, which backs the public `tx_pool_info` JSON-RPC endpoint, meaning every RPC call triggers an O(n) scan proportional to pool size.

## Finding Description
The `PoolMap` struct maintains all pool-wide statistics as incrementally-updated cached fields: [1](#0-0) 

`last_txs_updated_at` is the sole exception — computed by full scan every time: [2](#0-1) 

This is called unconditionally in `TxPoolService::info()`: [3](#0-2) 

`add_entry()` updates `total_tx_size` and `total_tx_cycles` incrementally but makes no update to any cached timestamp field: [4](#0-3) 

The pool is bounded by bytes (`max_tx_pool_size`, default 180 MB), not entry count. At a minimum transaction size of ~200 bytes, the pool can hold on the order of hundreds of thousands of entries. The `info()` call holds a read lock on `tx_pool` for the duration of the scan, which delays concurrent write operations (transaction submissions, evictions) while the scan runs.

## Impact Explanation
This is a **Low** severity finding: **any other important performance improvements for CKB** (501–2000 points). The O(n) scan degrades `tx_pool_info` RPC response latency proportionally to pool size and holds the pool read lock during the scan, delaying write-lock acquisition by concurrent operations. It does not crash the node, cause consensus deviation, or cause network-wide congestion. The impact is local RPC performance degradation.

## Likelihood Explanation
The `tx_pool_info` endpoint is public and unauthenticated. Triggering the expensive path requires a large pool, which can occur organically during network congestion or be induced by an attacker submitting many fee-paying transactions. The attacker cost is non-trivial (real CKB fees required to fill 180 MB), which limits the "few costs" threshold needed for a High rating. Any caller — including legitimate monitoring tools or miners — triggers the scan once the pool is large.

## Recommendation
Add a `last_txs_updated_at: u64` cached field to `PoolMap`, initialized to `0`. In `add_entry()`, update it with `self.last_txs_updated_at = self.last_txs_updated_at.max(entry.timestamp)`. On removal, if the removed entry held the maximum, either recompute once or maintain a secondary ordered structure. Replace `get_max_update_time()` with a direct field read. This makes the operation O(1), consistent with all other pool-wide statistics.

## Proof of Concept
1. Fill the tx pool to near `max_tx_pool_size` (180 MB default) by submitting many independent transactions with distinct UTXOs and minimum acceptable fee rates (~900,000 entries at 200 bytes each).
2. Repeatedly call `tx_pool_info` via JSON-RPC (e.g., `curl` in a loop or a script).
3. Observe that each call invokes `get_max_update_time()`, which iterates all ~900,000 entries. Measure RPC latency scaling linearly with pool entry count versus a near-empty pool.
4. Confirm by adding a counter or timing log inside `get_max_update_time()` to verify the iteration count matches pool size on every call.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L60-75)
```rust
pub struct PoolMap {
    /// The pool entries with different kinds of sort strategies
    pub(crate) entries: MultiIndexPoolEntryMap,
    /// All the deps, header_deps, inputs, outputs relationships
    pub(crate) edges: Edges,
    /// All the parent/children relationships
    pub(crate) links: TxLinksMap,
    pub(crate) max_ancestors_count: usize,
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
}
```

**File:** tx-pool/src/component/pool_map.rs (L130-136)
```rust
    pub(crate) fn get_max_update_time(&self) -> u64 {
        self.entries
            .iter()
            .map(|(_, entry)| entry.inner.timestamp)
            .max()
            .unwrap_or(0)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L210-219)
```rust
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
```

**File:** tx-pool/src/service.rs (L1093-1093)
```rust
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
```
