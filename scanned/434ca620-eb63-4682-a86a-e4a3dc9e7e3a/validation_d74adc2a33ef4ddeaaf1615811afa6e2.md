### Title
`PoolMap::total_tx_size` / `total_tx_cycles` Over-Counted When Ancestor-Eviction Occurs During `add_entry` - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's aggregate size and cycle counters (`total_tx_size`, `total_tx_cycles`) are pre-computed **before** ancestor-eviction runs, then unconditionally written back **after** eviction. Because eviction itself decrements those same counters via `update_stat_for_remove_tx`, the final write overwrites the decrements, leaving the counters permanently inflated by the total size/cycles of every evicted entry.

---

### Finding Description

`add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```
Step 1  – pre-compute new totals (before any mutation)
Step 2  – check_and_record_ancestors  ← may evict entries, decrementing self.total_tx_size
Step 3  – record_entry_edges          ← may fail (early return)
Step 4  – insert_entry
Step 5  – self.total_tx_size = total_tx_size   ← stale pre-computed value written back
``` [1](#0-0) 

Step 1 captures `total_tx_size = self.total_tx_size + entry.size` before any mutation. Step 2 (`check_and_record_ancestors`) may call `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which subtracts each evicted entry's size from `self.total_tx_size`: [2](#0-1) [3](#0-2) [4](#0-3) 

After Step 4 succeeds, Step 5 writes the stale pre-computed value back, erasing all decrements performed in Step 2.

**Correct post-insertion value:**
`original_total − Σ(evicted_sizes) + entry.size`

**Actual post-insertion value:**
`original_total + entry.size`

The eviction path in `check_and_record_ancestors` is reached when a new transaction's ancestor count exceeds `max_ancestors_count` but some ancestors are `cell_ref_parents` that can be evicted to make room: [5](#0-4) 

---

### Impact Explanation

**Impact: High** — wrong accounting of pool capacity.

`total_tx_size` is the sole counter used by `limit_size` to decide whether the pool is over its configured `max_tx_pool_size` and must evict transactions: [6](#0-5) 

An inflated `total_tx_size` causes the pool to believe it is larger than it actually is. Consequences:

1. **Spurious evictions** — `limit_size` will evict legitimate pending/proposed transactions that would otherwise fit, permanently degrading mempool throughput.
2. **False rejection of incoming transactions** — new transactions are rejected with `Reject::Full` even though actual pool occupancy is below the limit.
3. **Incorrect RPC reporting** — `get_tx_pool_info` returns an inflated `total_tx_size`, misleading operators and fee-estimation callers.
4. **Compounding drift** — each subsequent eviction-during-insertion event adds more phantom bytes, so the discrepancy grows monotonically until the node restarts.

This is directly analogous to the reference bug: a balance that is decremented (evicted entries removed) but the decrement is silently overwritten, leaving the tracked total higher than the true value.

---

### Likelihood Explanation

**Likelihood: Low** — requires a specific but reachable transaction graph.

The eviction branch in `check_and_record_ancestors` fires only when:
- A submitted transaction has ancestors that include `cell_ref_parents` (transactions referenced as cell-deps by the new tx's ancestors), **and**
- The total ancestor count exceeds `max_ancestors_count` (default 1 000), **but**
- Removing the `cell_ref_parents` would bring the count back within the limit.

An unprivileged transaction sender reachable via the `send_transaction` RPC or P2P relay can craft such a chain deliberately. No privileged access, key material, or majority hash power is required.

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` update to **after** all mutations complete, computing the final values from the actual post-eviction state rather than from a pre-mutation snapshot. Concretely, replace the pre-computed stale assignment with an incremental update that accounts for evictions:

```rust
// After insert_entry succeeds, add only the new entry's contribution
// (evictions have already decremented self.total_tx_size via update_stat_for_remove_tx)
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, call `recompute_total_stat()` after every `add_entry` that involved evictions, or restructure `add_entry` so that `updated_stat_for_add_tx` is called **after** `check_and_record_ancestors` completes.

---

### Proof of Concept

1. Pre-populate the pool with a chain of transactions `T1 → T2 → … → T_{N-1}` where `T1` is also referenced as a cell-dep by `T2` (making `T1` a `cell_ref_parent`). Set `N-1 = max_ancestors_count`.

2. Submit transaction `T_N` that spends `T_{N-1}`'s output. Its ancestor count is `N = max_ancestors_count + 1`, triggering the eviction branch.

3. `check_and_record_ancestors` evicts `T1` (and its descendants via `remove_entry_and_descendants`), calling `update_stat_for_remove_tx` for each, decrementing `self.total_tx_size` by their combined size `S_evicted`.

4. `record_entry_edges` succeeds; `insert_entry` inserts `T_N`.

5. Step 5 writes `self.total_tx_size = original_total + size(T_N)`, overwriting the `S_evicted` decrement.

6. Query `get_tx_pool_info`: `total_tx_size` is `S_evicted` bytes higher than the true sum of entries in the pool. Repeat to compound the drift. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/component/pool_map.rs (L235-250)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
    }
```

**File:** tx-pool/src/component/pool_map.rs (L598-628)
```rust
        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
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
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L710-729)
```rust
    /// Calculate size and cycles statistics for adding a tx.
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

**File:** tx-pool/src/component/pool_map.rs (L733-758)
```rust
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

**File:** tx-pool/src/pool.rs (L292-299)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
```
