### Title
`total_tx_size` Accounting Invariant Broken in `add_entry()` Due to Stale Pre-Eviction Value Overwrite — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry()`, the aggregate `total_tx_size` and `total_tx_cycles` counters are computed **before** `check_and_record_ancestors()` runs, but are written back **after** it returns — overwriting any decrements that occurred when `check_and_record_ancestors()` evicted entries via `remove_entry_and_descendants()`. This is a direct analog to the Gitcoin `userTotalStaked` invariant break: a summary counter is not kept in sync with the underlying per-entry data it is supposed to track.

---

### Finding Description

`PoolMap::add_entry()` follows this sequence:

```rust
// Step 1: snapshot new totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2: may evict cell-ref-parent txs, calling update_stat_for_remove_tx
//         which DECREMENTS self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3: OVERWRITE with the stale pre-eviction snapshot
self.total_tx_size = total_tx_size;    // ← ignores decrements from Step 2
self.total_tx_cycles = total_tx_cycles; // ← ignores decrements from Step 2
``` [1](#0-0) 

`updated_stat_for_add_tx()` computes `new_size = self.total_tx_size + entry.size` at the moment it is called. [2](#0-1) 

`check_and_record_ancestors()` can then call `remove_entry_and_descendants()` on `cell_ref_parents` — transactions that hold a cell dep on an output the new transaction wants to consume as an input — which calls `update_stat_for_remove_tx()` and correctly decrements `self.total_tx_size` for each evicted entry. [3](#0-2) 

`remove_entry()` calls `update_stat_for_remove_tx()`, which correctly decrements `self.total_tx_size`: [4](#0-3) [5](#0-4) 

But then `add_entry()` overwrites `self.total_tx_size` with the stale snapshot from Step 1, which does not account for the evictions. The invariant:

```
total_tx_size == sum(entry.size for all entries currently in pool)
```

is broken. After the overwrite, `total_tx_size` is inflated by exactly the sum of the sizes of all entries evicted during `check_and_record_ancestors()`.

---

### Impact Explanation

`total_tx_size` is the sole gate used by `limit_size()` to decide whether to evict transactions:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entry
}
``` [6](#0-5) 

With an inflated `total_tx_size`, `limit_size()` evicts additional legitimate transactions even though the pool has real capacity. Each such eviction decrements `total_tx_size` by the evicted entry's size, but the pool's actual occupancy is already below the limit. The net effect is that the pool's effective capacity shrinks by the total size of the entries evicted in `check_and_record_ancestors()` per triggering submission.

An attacker who repeatedly triggers this path progressively shrinks the effective pool, causing legitimate pending transactions to be expelled and delaying or preventing their confirmation.

---

### Likelihood Explanation

The eviction path inside `check_and_record_ancestors()` is reached when:

1. A submitted transaction has more than `max_ancestors_count` (default 25) in-pool ancestors, **and**
2. At least one of those ancestors is a `cell_ref_parent` — a pool transaction that holds a cell dep pointing to an output the new transaction wants to spend as an input — such that removing it brings the ancestor count within the limit. [7](#0-6) 

This scenario is reachable by any unprivileged tx-pool submitter via the standard `send_transaction` RPC. An attacker can craft a transaction chain of depth 25 (A→C1→…→C24), submit a separate transaction B that uses A's output as a cell dep, then submit a transaction D that spends C24's output and A's output as inputs. D has 26 ancestors (A, C1–C24, B), exceeding the limit by 1; B is a `cell_ref_parent` and gets evicted, triggering the overwrite bug. [8](#0-7) 

No privileged access, no majority hashpower, and no social engineering is required.

---

### Recommendation

Compute the new totals **after** `check_and_record_ancestors()` returns, not before. Replace the pre-computation pattern with a post-eviction increment:

```rust
// After check_and_record_ancestors, record_entry_edges, insert_entry, etc.
self.total_tx_size = self.total_tx_size
    .checked_add(entry.size)
    .expect("size overflow already checked");
self.total_tx_cycles = self.total_tx_cycles
    .checked_add(entry.cycles)
    .expect("cycles overflow already checked");
```

The overflow check (currently done in `updated_stat_for_add_tx`) should still be performed early to reject oversized submissions, but the actual write to `self.total_tx_size` / `self.total_tx_cycles` must happen after all evictions are complete so it reflects the true pool state.

---

### Proof of Concept

**Setup:**
- Pool `max_tx_pool_size = 1000`, `max_ancestors_count = 25`
- Pool currently holds 900 bytes of transactions; `total_tx_size = 900`

**Attack steps:**

1. Attacker submits tx A (creates outputs X and Y).
2. Attacker submits tx B (size 100, uses output X as a cell dep). `total_tx_size = 1000`.
3. Attacker submits chain A→C1→…→C24 (each spending the previous). `total_tx_size` grows.
4. Attacker submits tx D (size 50, spends C24's output and output X as inputs).
   - D has 26 ancestors (A, C1–C24, B) > 25 = `max_ancestors_count`.
   - B is a `cell_ref_parent`; `26 - 1 = 25

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

**File:** tx-pool/src/component/pool_map.rs (L515-554)
```rust
    // return (ancestors, parents, cell_ref_parents)
    // `cell_ref_parents` may be invalidate when the tx consuming the cell is submitted
    fn get_tx_ancenstors(
        &self,
        entry: &TransactionView,
    ) -> (
        HashSet<ProposalShortId>,
        HashSet<ProposalShortId>,
        HashSet<ProposalShortId>,
    ) {
        let mut parents: HashSet<ProposalShortId> =
            HashSet::with_capacity(entry.inputs().len() + entry.cell_deps().len());
        let mut cell_ref_parents: HashSet<ProposalShortId> = Default::default();

        for input in entry.inputs() {
            let input_pt = input.previous_output();
            if let Some(deps) = self.edges.deps.get(&input_pt) {
                cell_ref_parents.extend(deps.iter().cloned());
                parents.extend(deps.iter().cloned());
            }

            let id = ProposalShortId::from_tx_hash(&input_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }
        for cell_dep in entry.cell_deps() {
            let dep_pt = cell_dep.out_point();
            let id = ProposalShortId::from_tx_hash(&dep_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }

        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        (ancestors, parents, cell_ref_parents)
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
