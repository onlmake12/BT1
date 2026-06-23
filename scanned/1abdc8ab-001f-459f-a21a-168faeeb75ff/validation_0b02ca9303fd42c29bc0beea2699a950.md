### Title
`total_tx_size`/`total_tx_cycles` Inflated When Eviction Occurs During `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new aggregate statistics (`total_tx_size`, `total_tx_cycles`) are computed **before** any evictions take place, but written back **after** evictions have already decremented those same fields. This causes the pool's accounting variables to be permanently inflated by the size and cycles of every evicted entry, mirroring the `_lockedETH` pattern from the reference report.

---

### Finding Description

`PoolMap::add_entry` follows this sequence: [1](#0-0) 

1. **Line 210–211**: `updated_stat_for_add_tx` reads the current `self.total_tx_size` / `self.total_tx_cycles` and returns pre-computed new totals (`total_tx_size`, `total_tx_cycles`) — but does **not** write them yet. [2](#0-1) 

2. **Line 213**: `check_and_record_ancestors` is called. When the incoming transaction has too many ancestors due to `cell_ref_parents`, it calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which **immediately decrements** `self.total_tx_size` and `self.total_tx_cycles` for each evicted entry. [3](#0-2) [4](#0-3) 

3. **Lines 218–219**: The pre-computed values (which were calculated from the state **before** evictions) are written back unconditionally, overwriting the correctly-decremented values. [5](#0-4) 

**Concrete arithmetic:**

| Step | `self.total_tx_size` |
|---|---|
| Before `add_entry` | `S` |
| After `updated_stat_for_add_tx` (computed, not stored) | `S + new_size` |
| After eviction of entries totalling `E` bytes (stored immediately) | `S − E` |
| After final overwrite (line 218) | `S + new_size` ← **wrong** |
| Correct value | `S − E + new_size` |

The inflation per `add_entry` call that triggers eviction equals exactly `E` (the total serialized size of all evicted entries), and it accumulates across calls.

---

### Impact Explanation

`total_tx_size` and `total_tx_cycles` are the authoritative pool-size accounting fields exposed via RPC and used for admission control: [6](#0-5) 

- **Incorrect RPC reporting**: `get_tx_pool_info` returns inflated `total_tx_size` / `total_tx_cycles`, misleading wallets, explorers, and fee estimators.
- **Premature pool-full rejection**: `updated_stat_for_add_tx` uses `self.total_tx_size` to detect overflow. An inflated value causes legitimate transactions to be rejected with `Reject::Full` earlier than the actual pool capacity warrants, effectively a self-inflicted admission DoS.
- **Incorrect fee estimation**: `estimate_fee_rate` iterates pool entries directly, but any logic relying on `total_tx_cycles` for cycle-budget decisions will use a wrong baseline.

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` is triggered when:
1. A new transaction's ancestor count exceeds `max_ancestors_count`, **and**
2. Some of those ancestors are `cell_ref_parents` (transactions that reference the same output as a cell dep). [7](#0-6) 

An unprivileged transaction submitter can deliberately craft a chain of transactions where multiple pending transactions share a cell dep, then submit a new transaction that pushes the ancestor count over the limit. This is a reachable, attacker-controlled entry path requiring no special privilege — only the ability to submit transactions via RPC or P2P relay.

---

### Recommendation

Compute the new totals **after** `check_and_record_ancestors` returns, so that any eviction-driven decrements to `self.total_tx_size` / `self.total_tx_cycles` are already reflected:

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
    // Validate limits before mutating state, but do NOT store the result yet.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));
    // Compute final totals AFTER evictions have already updated self.total_tx_size/cycles.
    self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
    self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
    Ok((true, evicts))
}
```

---

### Proof of Concept

```
1. Pool starts empty: total_tx_size = 0, total_tx_cycles = 0.

2. Submit a chain of transactions T1→T2→…→T_N where T_i all share a common
   cell dep D, filling the pool to max_ancestors_count - 1 ancestors.
   total_tx_size = S (correct).

3. Submit T_new that:
   - Spends an output of T_N (making ancestor count = max_ancestors_count + 1)
   - Also references D as a cell dep (making T1..T_N all cell_ref_parents)
   
   add_entry is called:
     a. updated_stat_for_add_tx computes (S + new_size, ...) — stored in local vars.
     b. check_and_record_ancestors evicts T1 (size=E):
        self.total_tx_size = S - E  ← correct intermediate value
     c. self.total_tx_size = S + new_size  ← overwrites, losing the -E correction

4. Actual pool size = S - E + new_size bytes of entries.
   Reported/used total_tx_size = S + new_size bytes.
   Inflation = E bytes, permanent until pool is cleared.

5. Repeat step 3 to accumulate inflation until the pool rejects all new
   transactions with Reject::Full despite having available capacity.
```

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

**File:** tx-pool/src/service.rs (L1086-1096)
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
```
