Audit Report

## Title
`total_tx_size`/`total_tx_cycles` Permanently Inflated After Cell-Dep Ancestor Eviction in `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::add_entry`, a pre-eviction snapshot of `total_tx_size` and `total_tx_cycles` is captured before `check_and_record_ancestors` runs. When that function evicts cell-dep ancestor transactions via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, the decrements to `self.total_tx_size` and `self.total_tx_cycles` are immediately overwritten by the stale snapshot at lines 218–219. The counters are permanently inflated by the aggregate size and cycles of every evicted entry, causing `limit_size` to subsequently expel legitimate transactions from the pool with `Reject::Full` even though the pool is not actually full.

## Finding Description

**Root cause — `add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):**

`updated_stat_for_add_tx` is a `&self` (read-only) method that computes `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` and returns them as local variables — it does not mutate any field. [1](#0-0) 

These local values are captured at lines 210–211 before any eviction occurs: [2](#0-1) 

`check_and_record_ancestors` is then called at line 213. When the new entry's ancestor count exceeds `max_ancestors_count` but can be reduced by removing cell-dep parents, it calls `remove_entry_and_descendants` for each evict candidate: [3](#0-2) 

`remove_entry_and_descendants` delegates to `remove_entry`, which calls `update_stat_for_remove_tx` — correctly decrementing `self.total_tx_size` and `self.total_tx_cycles`: [4](#0-3) 

After all evictions complete, lines 218–219 unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction snapshot: [5](#0-4) 

The net effect: `self.total_tx_size` ends up as `(pre_eviction_total + entry.size)` instead of the correct `(pre_eviction_total - evicted_sizes + entry.size)`. The inflation equals the sum of sizes of all evicted entries and persists indefinitely.

**Downstream effect — `limit_size` (`tx-pool/src/pool.rs`, lines 292–329):**

`limit_size` uses `total_tx_size` as its sole eviction criterion: [6](#0-5) 

An inflated `total_tx_size` causes this loop to fire and expel legitimate pending/proposed transactions with `Reject::Full` even though the actual pool occupancy is within limits.

## Impact Explanation

Every time the cell-dep ancestor eviction path fires, `total_tx_size` is over-counted by the aggregate serialized size of the evicted transactions. Subsequent `limit_size` calls (invoked after every successful `submit_entry`) will then expel legitimate transactions. The inflation accumulates across multiple triggering submissions, progressively shrinking effective pool capacity. The `total_tx_size` value is also exposed via the `tx_pool_info` RPC, so callers receive incorrect telemetry. This constitutes a **High** impact: a vulnerability that can cause CKB network congestion (degraded transaction propagation) with relatively low cost to the attacker, matching the allowed impact class "Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (10001–15000 points).

## Likelihood Explanation

The eviction path requires a new transaction whose total ancestor count (including cell-dep parents) exceeds `max_ancestors_count` (default 1000), but whose non-cell-dep ancestor count is within the limit. An unprivileged attacker can construct this by:

1. Submitting a linear chain of ~1000 transactions to the pool.
2. Submitting a final transaction that references an intermediate transaction as a cell-dep, pushing the total ancestor count over the limit.

No privileged access, leaked keys, or majority hashpower is required — only the ability to call `send_transaction` via standard RPC. The setup cost is ~1000 fee-paying transactions per trigger, but the inflation effect accumulates across repeated submissions, making the attack progressively cheaper per unit of damage.

## Recommendation

Move the stat snapshot to **after** `check_and_record_ancestors` completes, so eviction-driven decrements are already reflected in `self.total_tx_size` before the new entry's contribution is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and assign AFTER all evictions:
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, use the existing `recompute_total_stat()` after all mutations for a correctness-first approach, though it is O(n) in pool size: [7](#0-6) 

## Proof of Concept

1. Configure a node with `max_ancestors_count = N` (default 1000).
2. Submit a root transaction `tx_root` spending a live cell.
3. Submit `N` child transactions forming a linear chain `tx_1 → tx_2 → … → tx_N`, each spending the previous output.
4. Submit `tx_dep` that uses `tx_root`'s output as a **cell-dep** (not an input).
5. Submit `tx_trigger` that spends `tx_N`'s output AND uses `tx_root`'s output as a cell-dep.

At step 5: `tx_trigger`'s ancestor count = N + 1 (cell-dep `tx_root`) + 1 (self) = N+2 > N. Since `cell_ref_parents = {tx_root}`, `ancestors_count - cell_ref_parents.len() = N+1 ≤ N+1`, so the eviction branch fires. `remove_entry_and_descendants(tx_root)` removes `tx_root` and `tx_dep`, calling `update_stat_for_remove_tx` twice. Lines 218–219 then overwrite `self.total_tx_size` with the pre-eviction snapshot, inflating it by `tx_root.size + tx_dep.size`.

**Observable verification:**
- Query `tx_pool_info` RPC: `total_tx_size` exceeds the sum of sizes of all entries actually in the pool.
- Submit additional transactions filling the pool to near `max_tx_pool_size`: `limit_size` evicts legitimate transactions with `Reject::Full` even though actual pool occupancy is below the limit.
- A unit test asserting `pool_map.total_tx_size == pool_map.recompute_total_stat().unwrap().0` after triggering the eviction path will fail, directly demonstrating the invariant violation. [8](#0-7)

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

**File:** tx-pool/src/component/pool_map.rs (L246-248)
```rust
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
```

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
    }
```

**File:** tx-pool/src/component/pool_map.rs (L711-729)
```rust
    fn updated_stat_for_add_tx(
        &self,
        tx_size: usize,
        cycles: Cycle,
    ) -> Result<(usize, Cycle), Reject> {
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
        let total_tx_cycles = self.total_tx_cycles.checked_add(cycles).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_cycles {} overflows by add {}",
                self.total_tx_cycles, cycles
            ))
        })?;
        Ok((total_tx_size, total_tx_cycles))
    }
```

**File:** tx-pool/src/pool.rs (L298-307)
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
```
