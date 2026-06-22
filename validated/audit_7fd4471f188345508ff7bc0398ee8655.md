### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwrites In-Place Updates After Ancestor Eviction — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's `total_tx_size` and `total_tx_cycles` accounting fields are pre-computed before a potential eviction step, then unconditionally written back after the eviction. Because `remove_entry` (called during ancestor eviction) already mutates those same fields in-place, the final assignment discards the eviction's effect and leaves the counters inflated. Any unprivileged user who submits a transaction that triggers the ancestor-eviction path can cause the pool to permanently over-report its size, leading to cascading unnecessary evictions of valid transactions.

---

### Finding Description

**Root cause — `add_entry` in `tx-pool/src/component/pool_map.rs` lines 200–221:**

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1 — snapshot: total_tx_size = self.total_tx_size + entry.size
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // line 210-211

    // Step 2 — may call remove_entry_and_descendants → remove_entry
    //           → update_stat_for_remove_tx, which DIRECTLY MUTATES
    //           self.total_tx_size and self.total_tx_cycles
    evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

    self.insert_entry(&entry, status);
    ...
    // Step 3 — OVERWRITES the in-place mutations from Step 2
    self.total_tx_size = total_tx_size;                             // line 218
    self.total_tx_cycles = total_tx_cycles;                         // line 219
    Ok((true, evicts))
}
``` [1](#0-0) 

**Step 1** (`updated_stat_for_add_tx`, lines 711–729) captures a snapshot of the future totals:

```
total_tx_size  = self.total_tx_size  + entry.size
total_tx_cycles = self.total_tx_cycles + entry.cycles
``` [2](#0-1) 

**Step 2** (`check_and_record_ancestors`, lines 588–640) may enter the eviction branch (lines 603–625) when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. Inside that branch it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` (lines 733–758), which **directly subtracts** the evicted entries' sizes and cycles from `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) [4](#0-3) 

**Step 3** (lines 218–219) then unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale snapshot from Step 1, erasing the subtractions performed in Step 2.

**Concrete trace:**

| State | `self.total_tx_size` | `self.total_tx_cycles` |
|---|---|---|
| Before (tx A in pool, size=100, cycles=1000) | 100 | 1000 |
| After Step 1 (new tx B, size=50, cycles=500) | snapshot=150 | snapshot=1500 |
| After Step 2 (tx A evicted via `update_stat_for_remove_tx`) | 0 | 0 |
| After Step 3 (overwrite with snapshot) | **150** ← wrong | **1500** ← wrong |
| Correct value (only tx B remains) | 50 | 500 |

The evicted entries' sizes are permanently lost from the accounting.

---

### Impact Explanation

1. **Inflated `total_tx_size`** causes `limit_size` (lines 298–328) to believe the pool is over its configured `max_tx_pool_size` when it is not, triggering a cascade of unnecessary evictions of valid, fee-paying transactions. [5](#0-4) 

2. **Inflated `total_tx_cycles`** causes `TxPoolInfo` (returned by the `tx_pool_info` RPC) to report incorrect values, misleading clients and fee estimators. [6](#0-5) 

3. The inflation is **permanent** until the pool is cleared or the node restarts, because every subsequent eviction-triggering submission compounds the error.

4. A sustained attack can keep the pool in a state where it continuously evicts legitimate transactions, effectively denying mempool service to honest users.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged user who submits a transaction via the `send_transaction` RPC or P2P relay that:

- References existing pool transactions as cell deps (making them `cell_ref_parents`), AND
- Has an ancestor count that satisfies: `ancestors_count > max_ancestors_count` AND `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` [7](#0-6) 

This is a normal, supported transaction pattern (cell deps referencing in-pool outputs). No privileged access, leaked keys, or majority hashpower is required. The attacker only needs to craft a valid transaction with the right dependency structure.

---

### Recommendation

Compute the pre-snapshot **after** `check_and_record_ancestors` returns (so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles`), then add only the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add the new entry's contribution AFTER evictions are settled
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, remove the pre-computation entirely and call `update_stat_for_add_tx` (a mutable in-place version) after `check_and_record_ancestors`, mirroring the pattern already used by `update_stat_for_remove_tx`.

---

### Proof of Concept

Deterministic reasoning (no external tooling required):

1. Pre-populate the pool with tx A (size=100, cycles=1000) as a `cell_ref_parent` candidate. `total_tx_size = 100`.
2. Submit tx B (size=50, cycles=500) with a cell dep on A's output, with `ancestors_count = max_ancestors_count + 1` and `cell_ref_parents = {A}`, satisfying the eviction branch condition at line 603.
3. `updated_stat_for_add_tx` snapshots `total_tx_size = 150` (line 210).
4. `check_and_record_ancestors` evicts A via `remove_entry_and_descendants` → `update_stat_for_remove_tx(100, 1000)` → `self.total_tx_size = 0` (line 247).
5. Line 218 sets `self.total_tx_size = 150` (the stale snapshot).
6. Pool now contains only tx B (size=50) but reports `total_tx_size = 150`.
7. `limit_size` loop at line 298 fires (`150 > max_tx_pool_size` if limit is, e.g., 100), evicting tx B even though the pool is actually under the limit.
8. Repeat submissions compound the inflation monotonically. [8](#0-7) [5](#0-4)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L69-75)
```rust
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

**File:** tx-pool/src/pool.rs (L292-328)
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
```
