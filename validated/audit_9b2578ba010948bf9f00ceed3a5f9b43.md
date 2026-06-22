### Title
`PoolMap::add_entry` Overwrites `total_tx_size`/`total_tx_cycles` With Pre-Eviction Values, Inflating Pool-Size Accounting - (File: tx-pool/src/component/pool_map.rs)

### Summary

In `PoolMap::add_entry`, the new cumulative `total_tx_size` and `total_tx_cycles` are computed **before** any ancestor-eviction side-effects occur, then written back **after** those side-effects have already decremented the same counters. The result is that every eviction triggered by `check_and_record_ancestors` is silently un-done, permanently inflating the pool's size and cycle accounting. An unprivileged tx-pool submitter can exploit this to make the node believe its mempool is larger than it really is, causing legitimate transactions to be spuriously evicted or rejected.

### Finding Description

`add_entry` in `PoolMap` follows this sequence:

```rust
// Step 1 – snapshot new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may evict entries; each eviction calls remove_entry →
//           update_stat_for_remove_tx, which DECREMENTS self.total_tx_size
//           and self.total_tx_cycles in place
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Step 3 – OVERWRITE with the pre-eviction snapshot, discarding the
//           decrements performed in Step 2
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`check_and_record_ancestors` evicts entries when a new transaction's ancestor count exceeds `max_ancestors_count` but can be brought within the limit by removing cell-dep-conflicting parents: [2](#0-1) 

Each evicted entry goes through `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) [4](#0-3) 

But the final write-back in `add_entry` (Step 3) restores the pre-eviction snapshot, so the decrements are lost. After the call, `self.total_tx_size` equals `(original total) + (new entry size)` instead of the correct `(original total) − (evicted sizes) + (new entry size)`. The inflation per triggering event equals the sum of all evicted entries' serialized sizes.

The same pattern exists in `VerifyQueue::remove_tx` / `add_tx`, but the primary exploitable path is through `PoolMap::add_entry`. [5](#0-4) 

### Impact Explanation

`total_tx_size` is the authoritative counter used by `limit_size` to enforce `max_tx_pool_size` (default 180 MB). When it is inflated, the pool believes it is fuller than it actually is and begins evicting legitimate pending/proposed transactions to bring the reported size back under the limit. Repeated triggering accumulates inflation without bound. Additionally, `updated_stat_for_add_tx` rejects new submissions with `Reject::Full` if `total_tx_size.checked_add(tx_size)` overflows `usize`; a sufficiently inflated counter can reach that threshold on 32-bit targets or after extreme accumulation on 64-bit targets. [6](#0-5) 

The practical result is a **tx-pool DoS**: honest users' transactions are evicted or rejected while the pool appears full, blocking further deposits into the mempool.

### Likelihood Explanation

Triggering the eviction branch requires:
1. A chain of `max_ancestors_count + 1` (default: 1001) transactions already in the pool.
2. At least one ancestor sharing a cell dep with the new submission, so `ancestors_count − cell_ref_parents.len() ≤ max_ancestors_count`.

Building a 1001-transaction chain is resource-intensive but feasible for a motivated attacker with a single initial UTXO. Each triggering event inflates `total_tx_size` by the serialized size of all evicted entries (potentially hundreds of KB per event). Accumulating enough inflation to cause visible DoS requires many iterations (~1 200 events to inflate by 180 MB at ~150 KB per event), making this a sustained but realistic attack. The code comment "cycles overflow is possible, currently obtaining cycles is not accurate" confirms the developers are already aware of related accounting fragility. [7](#0-6) 

### Recommendation

Compute `total_tx_size` and `total_tx_cycles` **after** all evictions have completed, not before. One safe approach:

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
    // Validate capacity BEFORE mutating state (no write yet)
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Re-compute AFTER evictions so the decrements are included
    self.total_tx_size = self.total_tx_size
        .checked_add(entry.size)
        .expect("size already validated above");
    self.total_tx_cycles = self.total_tx_cycles
        .checked_add(entry.cycles)
        .expect("cycles already validated above");
    Ok((true, evicts))
}
```

Alternatively, recompute both counters from scratch via `recompute_total_stat` after every `add_entry` call that produces evictions, similar to the recovery path already used in `update_stat_for_remove_tx`.

### Proof of Concept

1. Fund a wallet with one UTXO.
2. Submit a chain of 1 001 transactions (`tx_0 → tx_1 → … → tx_1000`), where `tx_500` includes a specific always-success cell dep `D`.
3. Submit `tx_new` that spends `tx_1000`'s output **and** also declares cell dep `D`.
   - `ancestors_count` = 1 001 > 1 000 (`max_ancestors_count`).
   - `cell_ref_parents` = {`tx_500`}, so `1001 − 1 = 1000 ≤ 1000`.
   - Eviction branch fires: `tx_500` through `tx_1000` (501 entries) are removed; `self.total_tx_size` is decremented by their combined size.
   - `add_entry` then writes back `total_tx_size = (pre-eviction total) + size(tx_new)`, erasing the 501-entry decrement.
4. Observe via `get_tx_pool_info` RPC that `total_tx_size` is inflated by ~501 × serialized-tx-size relative to the actual pool contents.
5. Repeat steps 1–4 ~1 200 times; `total_tx_size` exceeds `max_tx_pool_size`; the pool begins evicting honest transactions and rejecting new submissions with `PoolIsFull`. [8](#0-7) [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L588-639)
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

**File:** tx-pool/src/component/verify_queue.rs (L128-149)
```rust
    /// Remove a tx from the queue
    pub fn remove_tx(&mut self, id: &ProposalShortId) -> Option<Entry> {
        self.inner.remove_by_id(id).map(|e| {
            let tx_size = e.inner.tx.data().serialized_size_in_block();
            if let Some(total_tx_size) = self.total_tx_size.checked_sub(tx_size) {
                self.total_tx_size = total_tx_size;
            } else if let Some(total_tx_size) = self.recompute_total_tx_size() {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, recomputed {}",
                    self.total_tx_size, tx_size, total_tx_size
                );
                self.total_tx_size = total_tx_size;
            } else {
                error!(
                    "verify_queue total_tx_size {} underflowed by sub {}, and recomputing overflowed",
                    self.total_tx_size, tx_size
                );
            }
            self.shrink_to_fit();
            e.inner
        })
    }
```
