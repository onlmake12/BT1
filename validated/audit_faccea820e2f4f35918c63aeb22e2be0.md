### Title
`total_tx_size` / `total_tx_cycles` Drift in `add_entry` Due to Stale Pre-Computed Snapshot Overwriting Eviction Updates — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::add_entry` pre-computes the new `total_tx_size` and `total_tx_cycles` values before calling `check_and_record_ancestors`, which can itself evict entries (via `remove_entry_and_descendants`) and correctly decrement those counters. The pre-computed snapshot is then unconditionally written back, silently overwriting the decrements made during eviction. The result is that `total_tx_size` and `total_tx_cycles` become persistently inflated by the size and cycles of every entry evicted through the ancestor-limit path. `limit_size()` reads `total_tx_size` directly to decide whether to evict further transactions, so the inflated value causes it to expel legitimate pool entries that should not have been removed.

---

### Finding Description

In `PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`):

```rust
// Step A – snapshot taken BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step B – may call remove_entry_and_descendants → remove_entry
//           → update_stat_for_remove_tx, which mutates
//           self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ...

// Step C – stale snapshot overwrites the correct post-eviction values
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable at Step A. [2](#0-1) 

`check_and_record_ancestors` enters the eviction branch when the incoming transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by removing "cell-ref parents" (pool entries that reference the same cell dep as the new transaction). It calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size` and `self.total_tx_cycles`. [3](#0-2) 

`remove_entry` calls `update_stat_for_remove_tx` at line 247, which subtracts the evicted entry's `size` and `cycles` from `self.total_tx_size` / `self.total_tx_cycles`. [4](#0-3) 

At Step C, the stale local variables (computed before any eviction) are written back, erasing those decrements. After the call returns, `self.total_tx_size` equals `original_total + entry.size` instead of the correct `original_total − evicted_size + entry.size`. The drift accumulates with every transaction that triggers the eviction branch.

`limit_size` reads `self.pool_map.total_tx_size` directly and evicts entries until the value drops below `max_tx_pool_size`. [5](#0-4) 

Because the counter is inflated, `limit_size` sees a pool that appears larger than it really is and evicts additional legitimate transactions to compensate.

---

### Impact Explanation

- **Incorrect pool-size enforcement**: `total_tx_size` drifts upward with each eviction triggered through `check_and_record_ancestors`. `limit_size` uses this value as the sole criterion for further eviction, so it expels valid, fee-paying transactions that would otherwise remain in the pool.
- **Targeted transaction eviction**: An attacker who can predict which transactions will be evicted by `limit_size` (lowest fee-rate entries first) can use this drift to selectively push victim transactions out of the pool without paying the cost of filling the pool legitimately.
- **Persistent drift**: The inflation is never corrected unless `recompute_total_stat` is triggered by an underflow (which requires a separate removal path), so the error compounds across multiple crafted submissions.

---

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` is reachable by any unprivileged `send_transaction` RPC caller. The attacker must submit a transaction whose ancestor count exceeds `max_ancestors_count` (default 25) but whose excess ancestors are all "cell-ref parents" (pool entries that use the same out-point as a cell dep of the new transaction). This is a specific but fully attacker-controlled construction: the attacker pre-populates the pool with a chain of transactions that share a cell dep, then submits a transaction that references that dep and has a long ancestor chain. No privileged access, key material, or majority hash power is required.

---

### Recommendation

Move the stat update to **after** `check_and_record_ancestors` completes, so it reflects the post-eviction state of `self.total_tx_size` and `self.total_tx_cycles`:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and apply the stat increment only now, after all evictions are done
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, replace the pre-computed snapshot pattern with in-place increments (`self.total_tx_size += entry.size`) applied after `check_and_record_ancestors`, mirroring how `update_stat_for_remove_tx` applies decrements in-place.

---

### Proof of Concept

1. Pool starts empty; `max_tx_pool_size = 200_000` bytes; `max_ancestors_count = 25`.
2. Attacker submits 25 transactions `T1 → T2 → … → T25` (a chain), each also referencing a shared cell dep `C`. All 25 are accepted; `total_tx_size = 25 * S`.
3. Attacker submits `T26`, which spends `T25`'s output and also references `C`. Its ancestor count is 26, exceeding the limit. Because `T1…T25` are all cell-ref parents (they share dep `C`), the eviction branch fires: `T1` (lowest fee-rate) is evicted via `remove_entry_and_descendants`. `self.total_tx_size` is decremented to `24 * S` by `update_stat_for_remove_tx`.
4. Step C in `add_entry` writes back the stale snapshot: `self.total_tx_size = 25 * S + T26.size`. The actual pool holds 25 entries (`T2…T25` + `T26`) with true size `25 * S`, but the counter reads `25 * S + T26.size`.
5. `limit_size` is called. It sees `total_tx_size > max_tx_pool_size` (if the pool is near capacity) and evicts the next lowest-fee-rate entry — a victim transaction the attacker did not pay to displace.
6. Repeating step 3–5 accumulates drift of `T26.size` per iteration, allowing the attacker to continuously over-evict the pool. [6](#0-5) [7](#0-6) [8](#0-7)

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
