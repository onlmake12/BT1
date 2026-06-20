### Title
Stale Pre-Eviction Snapshot Overwrites Correctly-Decremented `total_tx_size`/`total_tx_cycles` in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's `total_tx_size` and `total_tx_cycles` accounting variables are computed from a snapshot taken **before** any in-function evictions occur, then unconditionally written back **after** those evictions have already correctly decremented the same variables. This overwrites the correct post-eviction state with a stale inflated value, permanently over-counting pool size and cycles for every transaction submission that triggers the ancestor-eviction path.

---

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

**Step 1 — snapshot taken before evictions (lines 210–211):**
```rust
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
```
`updated_stat_for_add_tx` simply returns `(self.total_tx_size + entry.size, self.total_tx_cycles + entry.cycles)` — a snapshot of the current totals plus the new entry, captured before any mutation. [1](#0-0) 

**Step 2 — evictions correctly decrement the live fields (line 213 → lines 616–621):**
```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
```
Inside `check_and_record_ancestors`, when the ancestor count exceeds `max_ancestors_count` but `cell_ref_parents` can be evicted to bring it under the limit, `remove_entry_and_descendants` is called for each evicted transaction. Each call chains into `remove_entry`, which calls `update_stat_for_remove_tx`, correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` for every evicted entry. [2](#0-1) [3](#0-2) 

**Step 3 — stale snapshot unconditionally overwrites the correctly-decremented live fields (lines 218–219):**
```rust
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```
These lines restore the pre-eviction snapshot, erasing every decrement performed in Step 2. The evicted transactions' sizes and cycles are now counted again in the pool totals even though those entries no longer exist in `self.entries`. [4](#0-3) 

The analogous structure to the reported Solidity bug is exact: one code path correctly updates the accounting variable (Step 2 via `update_stat_for_remove_tx`), and a second unconditional write at the end of the same function (Step 3) re-applies a stale value, producing a net double-count of the evicted entries.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict transactions from the pool:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    ...
    let removed = self.pool_map.remove_entry_and_descendants(&id);
    ...
}
``` [5](#0-4) 

Each time `add_entry` triggers the ancestor-eviction path, `total_tx_size` is inflated by the sum of sizes of all evicted transactions. This inflation is permanent and cumulative across submissions. A sufficiently inflated `total_tx_size` causes `limit_size` to evict legitimate pending/proposed transactions even when the actual pool occupancy is well below `max_tx_pool_size`, degrading pool liveness. `total_tx_cycles` is similarly inflated, corrupting fee-rate estimation. Both values are also exposed directly via the `get_pool_info` RPC, producing incorrect pool statistics visible to all clients.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged user who can submit transactions via the `send_transaction` RPC or the P2P relay protocol. The required condition is:

1. Several existing pool transactions use a common live cell as a `cell_dep` (creating `cell_ref_parents`).
2. A new transaction is submitted whose ancestor set (including those `cell_ref_parents`) exceeds `max_ancestors_count`, but whose ancestor count after removing the `cell_ref_parents` falls at or below the limit.

This is a normal transaction-graph pattern that arises organically in DeFi-style workloads (e.g., multiple transactions depending on a shared script cell). An attacker can also deliberately construct this pattern by first seeding the pool with a chain of transactions that share a cell dep, then submitting a transaction that references them, reliably triggering the eviction and inflating the counters on every such submission.

---

### Recommendation

Move the stat snapshot assignment to **after** `check_and_record_ancestors` completes, so it incorporates any eviction decrements, or apply the new-entry increment directly to the already-correct live fields instead of using a pre-computed snapshot:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    let mut evicts = Default::default();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, evicts));
    }
    // Validate that adding this entry won't overflow, but do NOT apply yet.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    // Evictions happen here; they correctly decrement self.total_tx_size/cycles.
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply the new-entry increment AFTER evictions have settled.
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

---

### Proof of Concept

Preconditions: pool has `max_ancestors_count = N`. Seed the pool with transactions `T1…TK` that all use cell `C` as a `cell_dep` (making them `cell_ref_parents` of any future transaction that also uses `C`). Submit a new transaction `Tnew` that:
- uses `C` as a `cell_dep`
- has a parent chain long enough that `ancestors_count > N` when `cell_ref_parents` are included, but `ancestors_count - |cell_ref_parents| <= N`

`add_entry` will:
1. Snapshot `total_tx_size_snap = total_tx_size + size(Tnew)` (Step 1).
2. Evict `T1…TK` via `remove_entry_and_descendants`, each call subtracting `size(Ti)` from `self.total_tx_size` (Step 2). After evictions: `self.total_tx_size = total_tx_size - Σsize(Ti)`.
3. Overwrite: `self.total_tx_size = total_tx_size_snap = total_tx_size + size(Tnew)` (Step 3).

Net result: `self.total_tx_size` is inflated by `Σsize(Ti) + size(Tnew)` above the true value. Repeating this submission pattern accumulates unbounded inflation, causing `limit_size` to evict legitimate transactions from an otherwise non-full pool. [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L200-221)
```rust
    pub(crate) fn add_entry(
        &mut self,
        mut entry: TxEntry,
        status: Status,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        let tx_short_id = entry.proposal_short_id();
        let mut evicts = Default::default();
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
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
        Ok((true, evicts))
    }
```

**File:** tx-pool/src/component/pool_map.rs (L244-247)
```rust
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
```

**File:** tx-pool/src/component/pool_map.rs (L613-625)
```rust
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
```

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L298-308)
```rust
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
```
