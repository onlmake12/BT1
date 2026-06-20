### Title
`total_tx_size`/`total_tx_cycles` Overwritten After Eviction Decrements in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the pool's aggregate accounting fields `total_tx_size` and `total_tx_cycles` are pre-computed before ancestor evictions occur, then unconditionally written back after those evictions have already correctly decremented the same fields. The evicted entries' sizes and cycles are silently lost from the tracked totals, causing `total_tx_size` and `total_tx_cycles` to be permanently inflated for the lifetime of the pool.

---

### Finding Description

`PoolMap::add_entry` executes the following sequence:

```rust
// Step 1: pre-compute new totals (old + new entry only)
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2: may evict entries via remove_entry_and_descendants,
//         which calls update_stat_for_remove_tx for each evicted tx,
//         correctly decrementing self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ... insert new entry ...

// Step 3: OVERWRITES the already-decremented fields with the
//         pre-computed value that ignores evictions
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

Inside `check_and_record_ancestors`, when `ancestors_count > max_ancestors_count` but `cell_ref_parents` can be evicted to make room, `remove_entry_and_descendants` is called for each evicted entry: [2](#0-1) 

`remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) [4](#0-3) 

After `check_and_record_ancestors` returns, `self.total_tx_size` correctly equals `S - E` (where `S` is the pre-eviction total and `E` is the total size of evicted entries). But lines 218–219 then overwrite it with the stale pre-computed value `S + entry.size`, discarding the eviction decrements entirely.

**Correct final value:** `S - E + entry.size`
**Actual final value:** `S + entry.size`

The fields are inflated by `E` (total size/cycles of all evicted entries) for every such eviction event.

---

### Impact Explanation

`total_tx_size` is the authoritative counter used by `limit_size` to decide whether to evict further transactions from the pool: [5](#0-4) 

An inflated `total_tx_size` causes `limit_size` to evict additional valid, fee-paying transactions that would otherwise fit within `max_tx_pool_size`. Each subsequent eviction-during-add event compounds the inflation further.

`total_tx_size` and `total_tx_cycles` are also directly exposed via the `tx_pool_info` RPC: [6](#0-5) 

Callers (wallets, fee estimators, monitoring tools) receive incorrect pool state. The pool may reject valid transactions with `Reject::Full` even when actual pool occupancy is below the configured limit.

---

### Likelihood Explanation

The vulnerable code path is triggered whenever a submitted transaction:
1. Has ancestors in the pool that exceed `max_ancestors_count` (default 125), AND
2. Some of those ancestors are `cell_ref_parents` (i.e., transactions that reference a cell dep that the new tx will consume), allowing eviction to proceed rather than returning `ExceededMaximumAncestorsCount`.

Any unprivileged tx-pool submitter can craft this scenario by first building a long chain of transactions that reference a shared cell dep, then submitting a transaction that consumes that cell dep. This is a realistic mempool usage pattern (e.g., UTXO consolidation chains with shared script deps). No special privileges, keys, or majority hashpower are required.

---

### Recommendation

Compute `total_tx_size` and `total_tx_cycles` **after** `check_and_record_ancestors` completes, so that eviction decrements are not overwritten:

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
    // Validate capacity before mutating state
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Apply the add AFTER evictions have already decremented the counters
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

Add a test that verifies `total_tx_size` and `total_tx_cycles` are correct after an `add_entry` call that triggers ancestor eviction.

---

### Proof of Concept

Let `max_ancestors_count = 2`, pool initially empty.

1. Submit `tx_A` (size=100, cycles=10) — pool: `total_tx_size=100`
2. Submit `tx_B` (size=100, cycles=10) with cell dep on `tx_A`'s output — pool: `total_tx_size=200`
3. Submit `tx_C` (size=50, cycles=5) with input from `tx_B` and cell dep on `tx_A`'s output.
   - `ancestors_count = 3 > max_ancestors_count = 2`
   - `cell_ref_parents = {tx_B}`, `ancestors_count - cell_ref_parents.len() = 2 <= 2` → eviction path
   - `remove_entry_and_descendants(tx_B)`: `self.total_tx_size = 200 - 100 = 100`
   - `self.total_tx_size = total_tx_size` (pre-computed) `= 200 + 50 = 250` ← **OVERWRITES**
   - Correct value: `100 - 100 + 50 = 50`; actual: `250`

`total_tx_size` is now inflated by 200 (the size of `tx_B` that was evicted). Subsequent calls to `limit_size` will evict `tx_A` and `tx_C` unnecessarily if `max_tx_pool_size < 250`. [7](#0-6) [8](#0-7) [9](#0-8)

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
