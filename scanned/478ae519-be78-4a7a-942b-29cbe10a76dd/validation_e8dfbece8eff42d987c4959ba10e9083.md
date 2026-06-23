### Title
`total_tx_size`/`total_tx_cycles` Overwritten with Stale Pre-Eviction Values After Ancestor Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

### Summary

In `PoolMap::add_entry`, the pool's `total_tx_size` and `total_tx_cycles` accounting variables are computed as local values before `check_and_record_ancestors` runs. When that function evicts entries via `remove_entry_and_descendants`, it correctly decrements `self.total_tx_size`/`self.total_tx_cycles` through `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-eviction local values, erasing the eviction's accounting effect. The result is that `total_tx_size` and `total_tx_cycles` are permanently overcounted by the size/cycles of the evicted transactions, causing `limit_size` to evict legitimate transactions unnecessarily and causing future valid submissions to be rejected with `Reject::Full` even when the pool has real capacity.

### Finding Description

In `PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`, lines 200–221):

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1: compute new totals into LOCAL variables
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    ...
    // Step 2: may evict entries, calling update_stat_for_remove_tx
    //         which MODIFIES self.total_tx_size / self.total_tx_cycles
    evicts = self.check_and_record_ancestors(&mut entry)?;
    ...
    // Step 3: OVERWRITES self.total_tx_size with the stale pre-eviction local value
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
``` [1](#0-0) 

`updated_stat_for_add_tx` (lines 711–729) reads `self.total_tx_size` at call time and returns `self.total_tx_size + entry.size` as a local variable: [2](#0-1) 

`check_and_record_ancestors` (lines 588–640) contains an eviction path triggered when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. In that branch it calls `self.remove_entry_and_descendants(next_id)`: [3](#0-2) 

`remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx` correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place: [4](#0-3) [5](#0-4) 

But immediately after `check_and_record_ancestors` returns, `add_entry` blindly assigns the stale local values back: [6](#0-5) 

**Concrete accounting drift:** If before `add_entry` `self.total_tx_size = X`, the new entry has size `S`, and evictions remove entries of total size `E`:
- Correct final value: `X - E + S`
- Actual final value: `X + S` (overcounted by `E`)

### Impact Explanation

`limit_size` (called after `add_entry` in the submission flow) uses `self.pool_map.total_tx_size` to decide whether to evict: [7](#0-6) 

With `total_tx_size` overcounted by `E`, `limit_size` will evict `E` bytes worth of additional legitimate transactions that should not have been removed. Simultaneously, future `add_entry` calls will fail with `Reject::Full` via `updated_stat_for_add_tx` even when the pool has real available capacity, because the inflated counter makes the pool appear full. The overcounting is permanent and cumulative — each eviction event during `add_entry` adds more drift.

The `TxPoolInfo` reported via RPC (`tx_pool_info`) also reads `total_tx_size` directly, so operators and monitoring systems see incorrect pool statistics: [8](#0-7) 

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is reachable by any unprivileged RPC caller (`send_transaction`) who can craft a transaction chain where:
1. A set of in-pool transactions are referenced as cell-deps by a new transaction's ancestors, making `ancestors_count > max_ancestors_count`
2. Removing those cell-dep-referencing ancestors brings the count back within limit

This is a normal, supported transaction pattern (cell-dep chains). No privileged access, leaked keys, or majority hashpower is required. The attacker only needs to submit a sequence of valid transactions through the public RPC.

### Recommendation

Recompute `total_tx_size` and `total_tx_cycles` **after** `check_and_record_ancestors` returns, rather than using the pre-eviction local values. The simplest fix is to move the stat update to after the eviction step:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Recompute AFTER evictions have already updated self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Or alternatively, validate the size limit before calling `check_and_record_ancestors` and do not cache the pre-eviction totals as locals that are later assigned back.

### Proof of Concept

1. Fill the pool with a chain of transactions `T1 → T2 → ... → Tn` where `Tn` is referenced as a cell-dep by a side transaction `S1`, making `S1`'s ancestor count exceed `max_ancestors_count`.
2. Submit a new transaction `T_new` whose ancestor set includes `S1` as a cell-dep parent, triggering the eviction branch in `check_and_record_ancestors` (lines 603–625).
3. `remove_entry_and_descendants(S1)` is called, decrementing `self.total_tx_size` by `size(S1)`.
4. `add_entry` then sets `self.total_tx_size = old_total + size(T_new)`, ignoring the `size(S1)` decrement.
5. Observe via `tx_pool_info` RPC that `total_tx_size` is now `size(S1)` bytes larger than the actual sum of entries in the pool.
6. Submit further valid transactions; they are rejected with `Reject::Full` despite the pool having real capacity, or observe that `limit_size` evicts additional legitimate transactions. [1](#0-0) [9](#0-8)

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

**File:** tx-pool/src/service.rs (L1083-1097)
```rust
        TxPoolInfo {
            tip_hash: tip_header.hash(),
            tip_number: tip_header.number(),
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
        }
```
