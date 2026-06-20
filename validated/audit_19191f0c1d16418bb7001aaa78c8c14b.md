### Title
Tx-Pool `total_tx_size`/`total_tx_cycles` Over-Counted When Evictions Occur During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the updated pool-size statistics (`total_tx_size`, `total_tx_cycles`) are pre-computed before any evictions take place. When `check_and_record_ancestors` evicts transactions via `remove_entry_and_descendants`, those evictions correctly decrement the running totals through `update_stat_for_remove_tx`. However, `add_entry` then unconditionally overwrites `self.total_tx_size` and `self.total_tx_cycles` with the stale pre-computed values, silently undoing the eviction decrements. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the aggregate size/cycles of every evicted transaction.

---

### Finding Description

In `PoolMap::add_entry` (`tx-pool/src/component/pool_map.rs`):

```
// Step 1 – snapshot totals BEFORE any evictions
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;   // lines 210-211

// Step 2 – may call remove_entry_and_descendants → remove_entry
//           → update_stat_for_remove_tx, which CORRECTLY decrements
//           self.total_tx_size / self.total_tx_cycles in-place
evicts = self.check_and_record_ancestors(&mut entry)?;          // line 213

// ... insert, record edges, etc. ...

// Step 3 – OVERWRITES the correctly-decremented values with the
//           stale snapshot from Step 1
self.total_tx_size  = total_tx_size;                            // line 218
self.total_tx_cycles = total_tx_cycles;                         // line 219
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is reached when a new transaction's ancestor count exceeds `max_ancestors_count` but the excess is attributable to cell-dep references (`cell_ref_parents`). In that case the code evicts the lowest-fee cell-dep parents to make room: [2](#0-1) 

Each eviction calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles` in-place: [3](#0-2) [4](#0-3) 

Because `updated_stat_for_add_tx` only *computes* (does not write) the new totals, and the write-back at lines 218-219 uses the pre-eviction snapshot, the net effect after one such `add_entry` call is:

```
actual total_tx_size  = (pre_add_total - evicted_sizes + new_tx_size)   ← correct
stored total_tx_size  = (pre_add_total               + new_tx_size)     ← inflated
```

The inflation accumulates across every `add_entry` call that triggers evictions.

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide when to evict transactions from the pool: [5](#0-4) 

An inflated `total_tx_size` causes `limit_size` to believe the pool is over-capacity when it is not, triggering unnecessary evictions of legitimate pending transactions. This degrades pool effectiveness and can prevent valid transactions from being confirmed in a timely manner. The incorrect value is also surfaced directly to external callers via the `tx_pool_info` RPC endpoint: [6](#0-5) 

---

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` is reachable by any unprivileged transaction sender. An attacker submits a set of transactions whose outputs are referenced as cell deps by a subsequent transaction. If the total ancestor count of that subsequent transaction exceeds `max_ancestors_count` (default 25) but the excess is entirely due to cell-dep parents, the eviction branch fires. No privileged access, key material, or majority hash power is required — only the ability to submit transactions to the pool via the standard RPC or P2P relay path.

---

### Recommendation

Move the write-back of `total_tx_size` / `total_tx_cycles` to *after* all evictions have completed, and base the final value on the post-eviction state rather than the pre-computed snapshot. One correct approach is to compute the delta (`+new_tx_size`, `-evicted_sizes`) and apply it atomically after `check_and_record_ancestors` returns, or to call `recompute_total_stat` after evictions when the evict set is non-empty.

---

### Proof of Concept

1. Fill the pool with 26 transactions `T1…T26` where `T2…T26` each reference an output of `T1` as a **cell dep** (making them all `cell_ref_parents` of any future transaction that also references `T1`'s output as a cell dep).
2. Submit a new transaction `T_new` that also references `T1`'s output as a cell dep. Its ancestor count is 26 (> `max_ancestors_count` = 25), but `cell_ref_parents.len()` = 25, so `26 - 25 = 1 ≤ 25` — the eviction branch fires.
3. One of `T2…T26` is evicted. `update_stat_for_remove_tx` decrements `self.total_tx_size` by `evicted.size`.
4. `add_entry` then overwrites `self.total_tx_size` with `pre_add_total + T_new.size`, ignoring the decrement.
5. Query `tx_pool_info` via RPC: `total_tx_size` is inflated by `evicted.size`. Repeat to accumulate unbounded inflation, causing `limit_size` to evict legitimate transactions from the pool. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/pool.rs (L557-572)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        target_to_be_committed: BlockNumber,
    ) -> Result<FeeRate, FeeEstimatorError> {
        if !(3..=131).contains(&target_to_be_committed) {
            return Err(FeeEstimatorError::NoProperFeeRate);
        }
        let fee_rate = self.pool_map.estimate_fee_rate(
            (target_to_be_committed - self.snapshot.consensus().tx_proposal_window().closest())
                as usize,
            self.snapshot.consensus().max_block_bytes() as usize,
            self.snapshot.consensus().max_block_cycles(),
            self.config.min_fee_rate,
        );
        Ok(fee_rate)
    }
```
