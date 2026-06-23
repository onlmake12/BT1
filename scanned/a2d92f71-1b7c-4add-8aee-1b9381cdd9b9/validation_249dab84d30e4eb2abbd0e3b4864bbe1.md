### Title
Stale Pre-Computed `total_tx_size`/`total_tx_cycles` Overwritten After Eviction in `add_entry` Corrupts Pool Accounting - (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new total pool size and cycles are computed **before** `check_and_record_ancestors` runs. That function can evict existing entries (correctly updating `self.total_tx_size`/`self.total_tx_cycles` via `update_stat_for_remove_tx`), but the stale pre-computed values are then unconditionally written back, silently discarding the eviction accounting. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the sizes/cycles of every evicted entry, corrupting all downstream pool-size enforcement and RPC reporting.

---

### Finding Description

`PoolMap::add_entry` follows this sequence:

```rust
// Step 1 – compute new totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

// Step 2 – may evict entries; each eviction calls update_stat_for_remove_tx,
//           which correctly subtracts from self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// ... insert new entry ...

// Step 3 – OVERWRITES the correctly-updated self.total_tx_size with the stale value
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` captures `self.total_tx_size + entry.size` into a local variable at the moment of the call. [2](#0-1) 

`check_and_record_ancestors` enters the eviction branch when `ancestors_count > max_ancestors_count` but `ancestors_count - cell_ref_parents.len() <= max_ancestors_count`. It calls `remove_entry_and_descendants`, which calls `remove_entry`, which calls `update_stat_for_remove_tx` — correctly decrementing `self.total_tx_size` and `self.total_tx_cycles` for each evicted entry. [3](#0-2) [4](#0-3) 

After `check_and_record_ancestors` returns, the correctly-updated `self.total_tx_size` is immediately overwritten with the stale pre-eviction value. The net effect is that `total_tx_size` ends up as `original + new_entry.size` instead of the correct `original − evicted_sizes + new_entry.size`. [5](#0-4) 

---

### Impact Explanation

`total_tx_size` and `total_tx_cycles` are the authoritative pool-size counters used by:

1. **`limit_size`** — evicts entries while `total_tx_size > max_tx_pool_size`. An inflated counter causes legitimate transactions to be evicted unnecessarily, degrading pool throughput and potentially enabling a targeted eviction of high-fee transactions. [6](#0-5) 

2. **`tx_pool_info` RPC** — reports `total_tx_size` and `total_tx_cycles` directly to callers. Inflated values mislead miners, wallets, and monitoring tools.

3. **Fee estimation** — pool-size state feeds into fee-rate estimation logic, so inflated counters can skew fee recommendations.

The inflation is permanent and cumulative: every subsequent eviction-triggering `add_entry` call adds more error to the counters, with no self-correcting mechanism unless the pool is fully cleared.

---

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` fires when a submitted transaction has more than `max_ancestors_count` (default 125) ancestors, but the excess is explained by `cell_ref_parents` — pool entries that reference the same cell deps as the new transaction. In practice, many transactions share popular cell deps (e.g., the secp256k1 lock script dep). An unprivileged tx-pool submitter can deliberately craft a long transaction chain that also references a popular cell dep already referenced by many pool entries, reliably triggering this path. No special privilege is required beyond the ability to call `send_transaction` via RPC. [7](#0-6) 

---

### Recommendation

Move the `updated_stat_for_add_tx` call (or the final assignment of `self.total_tx_size`/`self.total_tx_cycles`) to **after** `check_and_record_ancestors` completes, so that any eviction-driven decrements are already reflected in `self.total_tx_size` before the new entry's size is added:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and assign AFTER evictions have already updated self.total_tx_size
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the local variables entirely and perform the increment in-place after all mutations are complete.

---

### Proof of Concept

1. Fill the pool with 125+ transactions that all reference the same popular cell dep (e.g., secp256k1).
2. Submit a new transaction that (a) also references that cell dep and (b) has a long ancestor chain in the pool, pushing `ancestors_count` above `max_ancestors_count`.
3. `check_and_record_ancestors` enters the eviction branch, removes some `cell_ref_parents`, and correctly decrements `self.total_tx_size`.
4. `add_entry` then overwrites `self.total_tx_size` with the stale pre-eviction value.
5. Query `tx_pool_info` via RPC: `total_tx_size` will be larger than the sum of all actual entries' sizes, and `limit_size` will begin evicting legitimate transactions even though the pool has physical room. [8](#0-7)

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
