### Title
tx-pool `total_tx_size`/`total_tx_cycles` Inflated When Evictions Occur During `add_entry` - (File: tx-pool/src/component/pool_map.rs)

### Summary

In `pool_map.rs`, the `add_entry` function pre-computes updated size/cycle totals into local variables, then calls `check_and_record_ancestors` which may evict transactions (correctly updating `self.total_tx_size` and `self.total_tx_cycles` via `update_stat_for_remove_tx`). However, at the end of `add_entry`, the local pre-eviction values unconditionally overwrite `self.total_tx_size` and `self.total_tx_cycles`, discarding the eviction-driven decrements. The pool's running totals are permanently inflated by the size and cycles of every evicted transaction.

### Finding Description

The vulnerable sequence in `add_entry` (lines 200–220):

```rust
// Step 1 – totals computed BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may call remove_entry_and_descendants → update_stat_for_remove_tx,
//           which correctly decrements self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 – OVERWRITES the post-eviction self.total_tx_size with the
//           pre-eviction snapshot, erasing the decrements from Step 2
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when the incoming transaction's ancestor count exceeds `max_ancestors_count` but can be brought under the limit by removing "cell-ref parent" transactions (those that reference the same cell deps as the new tx). Each evicted entry is removed via `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which subtracts the evicted entry's `size` and `cycles` from `self.total_tx_size` / `self.total_tx_cycles`. [2](#0-1) 

`update_stat_for_remove_tx` performs the correct decrement on `self.*`, but those fields are then unconditionally overwritten by the stale local variables at the end of `add_entry`. [3](#0-2) 

### Impact Explanation

`total_tx_size` is the primary guard used by `limit_size` to decide whether to evict further transactions:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    ...
    let removed = self.pool_map.remove_entry_and_descendants(&id);
    ...
}
``` [4](#0-3) 

With an inflated `total_tx_size`, `limit_size` will believe the pool is over its size budget and will evict additional valid pending/proposed transactions that should not be removed. Each subsequent `add_entry` call that triggers the eviction path compounds the inflation. The pool's RPC-reported `total_tx_size` and `total_tx_cycles` also become incorrect, misleading operators and fee-estimation logic. [5](#0-4) 

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` is reachable by any unprivileged RPC caller or P2P peer that submits transactions to the tx-pool. An attacker can deliberately craft a chain of transactions that:
1. Fills the ancestor count close to `max_ancestors_count`.
2. References a cell dep already referenced by existing pool transactions (creating "cell-ref parents").
3. Submits a new transaction that triggers the eviction of those cell-ref parents.

This is a standard tx-pool submission path with no privilege requirement. The default `max_ancestors_count` is 25, which is easily reachable with a crafted transaction chain.

### Recommendation

Move the assignment of `self.total_tx_size` and `self.total_tx_cycles` to **before** `check_and_record_ancestors` is called, or recompute the totals after all evictions have completed. The simplest correct fix is:

```rust
// Apply the addition immediately so subsequent eviction decrements are not lost
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;

evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Remove the duplicate assignments that were here
```

Alternatively, call `recompute_total_stat()` at the end of `add_entry` whenever `evicts` is non-empty, similar to the recovery path already present in `update_stat_for_remove_tx`. [6](#0-5) 

### Proof of Concept

1. Pre-populate the pool with transactions `T_dep_1 … T_dep_N` that all reference the same cell dep `C`. Each has size `S_dep`.
2. Build a transaction chain `A_1 → A_2 → … → A_{max-1}` where `A_{max-1}` also references cell dep `C` (making it a "cell-ref parent").
3. Submit a new transaction `B` that spends an output of `A_{max-1}` and also references `C`. Its ancestor count is `max_ancestors_count`, triggering the eviction branch.
4. `check_and_record_ancestors` evicts `A_{max-1}` (and its descendants), calling `update_stat_for_remove_tx(S_dep, cycles_dep)`, decrementing `self.total_tx_size` by `S_dep`.
5. `add_entry` then overwrites `self.total_tx_size` with the pre-eviction snapshot `+ B.size`, so the decrement of `S_dep` is lost.
6. Observe via `get_tx_pool_info` RPC that `total_tx_size` is inflated by `S_dep`. Repeated submissions compound the inflation until `limit_size` begins evicting unrelated valid transactions. [7](#0-6)

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

**File:** tx-pool/src/pool.rs (L298-327)
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
        self.pool_map.entries.shrink_to_fit();
```

**File:** tx-pool/src/service.rs (L1086-1097)
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
        }
```
