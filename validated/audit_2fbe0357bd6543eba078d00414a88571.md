### Title
`add_entry` in `PoolMap` Overwrites Updated `total_tx_size`/`total_tx_cycles` Accumulators After Eviction — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool-wide size and cycles accumulators (`total_tx_size`, `total_tx_cycles`) are computed into local variables **before** `check_and_record_ancestors` runs. When that function evicts entries (via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`), it correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. However, `add_entry` then unconditionally overwrites those fields with the stale pre-eviction local values, erasing the decrements. The accumulators are left permanently inflated by the sizes and cycles of the evicted entries.

---

### Finding Description

`PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221) follows this sequence:

```
1. (line 210-211) local (total_tx_size, total_tx_cycles) = self.total_tx_size + entry.size/cycles
2. (line 213)     evicts = self.check_and_record_ancestors(&mut entry)?
                    └─> remove_entry_and_descendants(next_id)
                          └─> remove_entry(id)
                                └─> self.update_stat_for_remove_tx(size, cycles)
                                      └─> self.total_tx_size -= evicted.size   ← correct decrement
                                          self.total_tx_cycles -= evicted.cycles
3. (line 218-219) self.total_tx_size  = total_tx_size   ← OVERWRITES with stale value
                  self.total_tx_cycles = total_tx_cycles ← OVERWRITES with stale value
```

`check_and_record_ancestors` evicts entries only when the incoming transaction's ancestor count exceeds `max_ancestors_count` **and** the excess is entirely attributable to "cell-ref parents" (pool entries referenced via `cell_dep`, not `input`). In that case it calls `remove_entry_and_descendants` on those cell-ref parents (lines 616–621), which internally calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size`. But `add_entry` then blindly assigns the stale local snapshot back to `self.total_tx_size` (line 218), discarding those decrements.

After the operation, `self.total_tx_size` equals `(pre-add value) + entry.size` instead of the correct `(pre-add value) - (sum of evicted sizes) + entry.size`. The pool permanently believes it holds more bytes than it actually does.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size()` (`tx-pool/src/pool.rs`, line 298):

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee-rate entries
}
```

An inflated `total_tx_size` causes `limit_size()` to evict additional legitimate transactions that are well within the real pool capacity. Each subsequent call to `add_entry` that triggers the eviction path compounds the inflation. Over time, or with repeated crafted submissions, the pool can be driven to evict all pending transactions even though the actual byte occupancy is far below `max_tx_pool_size`. This constitutes a **transaction-pool denial-of-service**: valid user transactions are silently dropped, preventing them from being proposed or committed.

`total_tx_cycles` is similarly inflated, corrupting the `TxPoolInfo` RPC response and any cycle-based admission logic that reads it.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged RPC caller via `send_transaction`. The attacker needs to:

1. Submit a chain of transactions where at least one pool entry is referenced as a `cell_dep` by another pool entry (creating a "cell-ref parent" relationship).
2. Submit a new transaction whose total ancestor count exceeds `max_ancestors_count` (default 25), but where the excess is covered by cell-ref parents.

Both conditions are achievable with ordinary CKB transactions and no special privileges. The cell-dep reference pattern is a standard CKB feature used by scripts that share code cells. The inflation is permanent per triggering event and accumulates across multiple such submissions.

---

### Recommendation

Compute the final accumulator values **after** `check_and_record_ancestors` completes, not before. Replace the pre-computation pattern with a post-eviction update:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, Default::default()));
    }
    // Validate capacity headroom before mutating state
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply the addition AFTER evictions have already decremented the counters
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

Alternatively, recompute from scratch via `recompute_total_stat()` after the eviction step, though the incremental fix above is cheaper.

---

### Proof of Concept

**Setup**: pool with `max_ancestors_count = 25`, `max_tx_pool_size = 10 MB`.

1. Submit 25 transactions `T1…T25` forming a chain where `T25` references `T1`'s output as a `cell_dep` (making `T1` a cell-ref parent of `T25`). Pool `total_tx_size` = sum of their sizes (say 250 KB).

2. Submit `T26` whose inputs spend `T25`'s output. `T26`'s ancestor set = `{T1…T25}` = 25 entries, which equals `max_ancestors_count`. No eviction yet.

3. Submit `T27` whose inputs spend `T26`'s output AND also references `T1` as a `cell_dep`. Now `T27`'s ancestor count = 26 > 25. `cell_ref_parents = {T1}`. Since `26 - 1 = 25 <= max_ancestors_count`, the eviction branch fires: `T1` (and its descendants `T2…T26`) are removed via `remove_entry_and_descendants`, correctly decrementing `self.total_tx_size` by ~250 KB. But then line 218 overwrites `self.total_tx_size` back to `250 KB + size(T27)`.

4. The pool now reports ~250 KB used even though it contains only `T27`. Every subsequent `limit_size()` call will evict real transactions to satisfy a phantom size constraint.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
