### Title
`PoolMap::add_entry` Overwrites Correctly-Decremented `total_tx_size`/`total_tx_cycles` With Stale Pre-Computed Value After Cell-Dep Ancestor Eviction — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the updated pool-size statistics (`total_tx_size`, `total_tx_cycles`) are pre-computed **before** `check_and_record_ancestors` is called. When that function evicts entries via `remove_entry_and_descendants`, each eviction correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. However, the stale pre-computed values (which do not account for those evictions) are then unconditionally written back, permanently inflating both counters by the sum of the evicted entries' sizes and cycles.

---

### Finding Description

In `add_entry` (`tx-pool/src/component/pool_map.rs`):

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // (1) Pre-compute: total_tx_size = self.total_tx_size + entry.size
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    // (2) May evict entries via remove_entry_and_descendants → remove_entry
    //     → update_stat_for_remove_tx, which DECREMENTS self.total_tx_size
    evicts = self.check_and_record_ancestors(&mut entry)?;

    ...
    // (3) Overwrites self.total_tx_size with the stale pre-computed value,
    //     undoing all decrements from step (2)
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced to within limits by evicting "cell-ref parents" — in-pool transactions that use as a cell dep an output that the new transaction is spending as an input: [2](#0-1) 

Each eviction calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`: [3](#0-2) [4](#0-3) 

But the pre-computed `total_tx_size` (= `old_total + entry.size`, computed before any evictions) is then written back unconditionally, overwriting the correctly-decremented value. After the call, `self.total_tx_size` equals `old_total + entry.size` instead of the correct `old_total + entry.size − Σ(evicted_entry.size)`.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide whether to evict transactions from the pool:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
``` [5](#0-4) 

`limit_size` is called immediately after every successful `_submit_entry`: [6](#0-5) 

With an inflated `total_tx_size`, the pool believes it is over capacity when it is not, causing it to evict additional legitimate pending/proposed transactions unnecessarily. The inflated value also propagates to the `tx_pool_info` RPC response (`total_tx_size`, `total_tx_cycles` fields), giving operators and fee-estimators incorrect data. [7](#0-6) 

---

### Likelihood Explanation

The vulnerable path requires:
1. In-pool transactions that use some live cell `O` as a **cell dep**.
2. A new transaction that **spends** `O` as an input, making those in-pool transactions "cell-ref parents."
3. The total ancestor count of the new transaction (including cell-ref parents) exceeds `max_ancestors_count`, but evicting the cell-ref parents brings it within limits.

This is a realistic scenario in CKB: a widely-used code cell (e.g., a lock script binary) is referenced as a dep by many in-pool transactions, and a transaction spending that cell is submitted. Any unprivileged tx-pool submitter (via `send_transaction` RPC or P2P relay) can craft such a transaction chain. No special privilege is required.

---

### Recommendation

Move the stat update **after** `check_and_record_ancestors` completes, so it accounts for all evictions:

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
    // Validate that adding entry.size won't overflow, but don't apply yet
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Apply the new entry's contribution AFTER evictions have already
    // decremented the counters, so the final value is correct.
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

---

### Proof of Concept

1. Fill the pool with transactions T1…Tk that all use live cell `C` as a cell dep, forming a chain of depth `max_ancestors_count − 1`. Each T_i has size `S`.
2. Submit a new transaction T_new that **spends** cell `C` as an input. T_new's ancestor count (via cell-ref parents T1…Tk) exceeds `max_ancestors_count`.
3. `check_and_record_ancestors` evicts T1…Tk (total evicted size = `k × S`).
4. The pre-computed `total_tx_size` (= `old_total + T_new.size`) is written back, ignoring the `k × S` decrement.
5. `total_tx_size` is now inflated by `k × S`.
6. `limit_size` immediately fires and evicts `k × S / avg_tx_size` additional legitimate transactions from the pool, even though the pool is actually under capacity.
7. `tx_pool_info` RPC returns the inflated `total_tx_size` value. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
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
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```

**File:** tx-pool/src/process.rs (L149-153)
```rust
                tx_pool.remove_conflict(&entry.proposal_short_id());
                tx_pool
                    .limit_size(&self.callbacks, Some(&entry.proposal_short_id()))
                    .map_or(Ok(()), Err)?;

```

**File:** util/types/src/core/tx_pool.rs (L336-338)
```rust
    pub total_tx_size: usize,
    /// Total consumed VM cycles of all the transactions in the pool.
    pub total_tx_cycles: Cycle,
```
