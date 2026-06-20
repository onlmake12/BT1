### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites Correct Values After Eviction in `add_entry` - (File: tx-pool/src/component/pool_map.rs)

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` values are pre-computed from the current pool state before `check_and_record_ancestors` is called. When that function evicts transactions (via `remove_entry_and_descendants` → `update_stat_for_remove_tx`), it correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. However, lines 218–219 then unconditionally overwrite those correctly-updated fields with the stale pre-computed values, erasing the eviction accounting. The result is a persistent overestimation of pool resource usage that any unprivileged tx-pool submitter can trigger.

### Finding Description

In `add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1: pre-compute new totals from current self.total_tx_size / self.total_tx_cycles
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

    // Step 2: may evict transactions, calling update_stat_for_remove_tx internally
    evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

    self.insert_entry(&entry, status);
    ...
    // Step 3: overwrite self fields with the STALE pre-computed values
    self.total_tx_size = total_tx_size;                             // line 218
    self.total_tx_cycles = total_tx_cycles;                         // line 219
    Ok((true, evicts))
}
```

`updated_stat_for_add_tx` (lines 711–729) computes the new totals as local variables based on the state **before** any evictions: [1](#0-0) 

`check_and_record_ancestors` (lines 588–640) enters the eviction branch when `ancestors_count > self.max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= self.max_ancestors_count`. It calls `remove_entry_and_descendants`: [2](#0-1) 

`remove_entry_and_descendants` → `remove_entry` calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

After `check_and_record_ancestors` returns, lines 218–219 blindly write the stale pre-computed values back, erasing the correct decrements: [4](#0-3) 

**Concrete example:**
- Initial: `total_tx_size = 1000`, `total_tx_cycles = 5000`
- New tx: `size = 100`, `cycles = 500`
- Line 210–211: local `total_tx_size = 1100`, `total_tx_cycles = 5500`
- `check_and_record_ancestors` evicts a tx with `size = 200`, `cycles = 1000`
  - `update_stat_for_remove_tx` sets `self.total_tx_size = 800`, `self.total_tx_cycles = 4000`
- Lines 218–219: `self.total_tx_size = 1100` (stale), `self.total_tx_cycles = 5500` (stale)
- **Expected:** `900` / `4500`; **Actual:** `1100` / `5500` — overestimated by the evicted tx's footprint

### Impact Explanation

`total_tx_size` and `total_tx_cycles` are the authoritative pool resource counters. They are:
1. Reported directly via the `tx_pool_info` RPC (`TxPoolInfo::total_tx_size`, `TxPoolInfo::total_tx_cycles`), causing callers (wallets, fee estimators, monitoring tools) to see inflated pool usage.
2. Used as the base for subsequent `updated_stat_for_add_tx` calls, meaning every future admission check operates on an inflated baseline. If pool-size enforcement elsewhere compares `total_tx_size` against `max_tx_pool_size`, valid transactions will be incorrectly rejected as if the pool were fuller than it is.
3. The overestimation is **permanent** (persists until pool clear or node restart) and **cumulative** — each eviction event adds another layer of inflation. [5](#0-4) [6](#0-5) 

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when a submitted transaction has ancestors that include "cell-ref parents" (transactions that use a cell dep whose output is consumed by the new tx). This is a normal, reachable scenario: any unprivileged tx-pool submitter can craft a transaction that references a cell dep already consumed by an in-pool transaction, causing the eviction branch to fire. No privileged access, key material, or majority hashpower is required. [7](#0-6) 

### Recommendation

Move the assignment of `self.total_tx_size` and `self.total_tx_cycles` to **after** `check_and_record_ancestors` completes, and compute the new totals from the **post-eviction** state rather than pre-computing them before evictions occur. Specifically, replace the pre-computation pattern with a post-eviction addition:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
// Now self.total_tx_size and self.total_tx_cycles reflect evictions correctly.
// Add the new entry's contribution on top of the already-updated values.
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

### Proof of Concept

1. Submit tx A to the pool (pending). A uses output `X` as a cell dep.
2. Submit tx B to the pool (pending). B consumes output `X` as an input, making A a "cell-ref parent" of B.
3. Submit tx C, which depends on B as a parent (making B an ancestor of C) and also has enough other ancestors to push `ancestors_count > max_ancestors_count` while `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`.
4. `add_entry` for C: pre-computes `total_tx_size = current + C.size` at line 210.
5. `check_and_record_ancestors` evicts A (and its descendants) via `remove_entry_and_descendants`, calling `update_stat_for_remove_tx` which decrements `self.total_tx_size` by A's size.
6. Lines 218–219 overwrite `self.total_tx_size` with the stale pre-computed value, ignoring A's eviction.
7. Query `tx_pool_info` RPC: `total_tx_size` is inflated by A's serialized size. Repeat to accumulate unbounded inflation. [8](#0-7) [9](#0-8) [3](#0-2)

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

**File:** tx-pool/src/component/pool_map.rs (L588-640)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

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

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
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

**File:** tx-pool/src/service.rs (L1086-1096)
```rust
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
```
