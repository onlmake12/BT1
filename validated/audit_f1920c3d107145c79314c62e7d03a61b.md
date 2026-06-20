### Title
`total_tx_size`/`total_tx_cycles` Accumulators Inflated When Evictions Occur During `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` values are computed **before** any evictions take place, then written back **after** evictions have already decremented those same fields. This causes the pool's running size/cycle totals to be permanently inflated by the sizes and cycles of every evicted transaction, mirroring the Astaria `yIntercept` accounting bug where a running accumulator is not decremented when a partial operation occurs.

---

### Finding Description

`PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

1. **Line 210–211**: `updated_stat_for_add_tx` snapshots `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` into local variables `total_tx_size` / `total_tx_cycles`.
2. **Line 213**: `check_and_record_ancestors` is called. When the new entry's ancestor count exceeds `max_ancestors_count` due to cell-dep references, it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **decrements `self.total_tx_size` and `self.total_tx_cycles`** for each evicted transaction.
3. **Lines 218–219**: The pre-eviction snapshot values are written back unconditionally, **overwriting** the decrements performed in step 2. [1](#0-0) 

The eviction path inside `check_and_record_ancestors`: [2](#0-1) 

Each eviction calls `remove_entry`, which calls `update_stat_for_remove_tx`: [3](#0-2) 

The decrement in `update_stat_for_remove_tx`: [4](#0-3) 

After `add_entry` returns, `self.total_tx_size` equals `(original_total) + (new_entry_size)` instead of the correct `(original_total) - (sum_of_evicted_sizes) + (new_entry_size)`. The evicted transactions' sizes and cycles are never subtracted from the running totals.

---

### Impact Explanation

**`total_tx_size` is inflated** by the aggregate size of all evicted transactions. This has three concrete downstream effects:

1. **Over-eviction via `limit_size`**: `TxPool::limit_size` loops while `self.pool_map.total_tx_size > self.config.max_tx_pool_size`. An inflated counter causes the pool to evict additional legitimate transactions that would not otherwise be removed, degrading pool quality and denying service to honest users. [5](#0-4) 

2. **Wrong fee-rate estimation**: `PoolMap::estimate_fee_rate` and `TxPool::estimate_fee_rate` use `total_tx_size` and `total_tx_cycles` to simulate block packing. Inflated values produce fee-rate estimates that are too high, misleading wallets and users. [6](#0-5) 

3. **Incorrect `get_tx_pool_info` RPC output**: `TxPoolInfo.total_tx_size` and `TxPoolInfo.total_tx_cycles` are read directly from these fields and returned to any RPC caller. [7](#0-6) 

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when a submitted transaction references a cell dep whose producing transaction is already in the pool and the resulting ancestor count exceeds `max_ancestors_count`. An unprivileged tx-pool submitter can deliberately craft such a transaction:

- Submit a chain of transactions up to the ancestor limit.
- Submit a new transaction that references one of those transactions as a **cell dep** (not just an input), making it a `cell_ref_parent`.
- The condition `ancestors_count.saturating_sub(cell_ref_parents.len()) <= max_ancestors_count` is satisfied, triggering the eviction loop. [8](#0-7) 

This is reachable by any node that accepts transactions from the network (default configuration). The attacker pays only the minimum fee for the crafted transactions.

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` assignment to **after** `check_and_record_ancestors` completes, and base the new totals on the **post-eviction** `self.total_tx_size` rather than the pre-eviction snapshot:

```rust
// After check_and_record_ancestors (which may have decremented self.total_tx_size):
self.total_tx_size = self.total_tx_size.checked_add(entry.size)
    .expect("already checked above");
self.total_tx_cycles = self.total_tx_cycles.checked_add(entry.cycles)
    .expect("already checked above");
```

Alternatively, recompute the totals after all mutations using `recompute_total_stat` as a correctness check, similar to the fallback already present in `update_stat_for_remove_tx`. [9](#0-8) 

---

### Proof of Concept

**Setup**: `max_ancestors_count = 25`, pool is empty.

1. Submit transactions `T1 … T24` forming a chain (each spends the previous output). All 24 are accepted; `total_tx_size = sum(sizes_T1..T24)`.
2. Submit transaction `T25` that **cell-dep references** `T1`'s output (making `T1` a `cell_ref_parent`). The ancestor count for `T25` would be 25 (T1–T24 + T25 itself), exceeding `max_ancestors_count = 25`.
3. `check_and_record_ancestors` enters the eviction branch. It calls `remove_entry_and_descendants(T1)`, which removes `T1` through `T24` (all descendants), calling `update_stat_for_remove_tx` 24 times, decrementing `self.total_tx_size` by `sum(sizes_T1..T24)`.
4. Back in `add_entry`, line 218 writes `self.total_tx_size = total_tx_size` (the pre-eviction snapshot = `sum(sizes_T1..T24) + size_T25`).
5. **Result**: Pool contains only `T25`, but `total_tx_size = sum(sizes_T1..T24) + size_T25` instead of the correct `size_T25`. The counter is inflated by `sum(sizes_T1..T24)`.
6. `limit_size` now sees `total_tx_size > max_tx_pool_size` and begins evicting `T25` and any subsequently submitted legitimate transactions. [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L235-249)
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
```

**File:** tx-pool/src/component/pool_map.rs (L334-358)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
```

**File:** tx-pool/src/component/pool_map.rs (L603-625)
```rust
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

**File:** tx-pool/src/pool.rs (L298-326)
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
```

**File:** tx-pool/src/service.rs (L1089-1090)
```rust
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
