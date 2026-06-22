### Title
Stale Pre-Eviction Totals Overwrite Correct Accounting in `add_entry`, Inflating `total_tx_size`/`total_tx_cycles` - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` are computed **before** `check_and_record_ancestors` runs. When that function evicts entries (calling `remove_entry_and_descendants` → `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size`/`self.total_tx_cycles` in-place), the stale pre-eviction local variables are then unconditionally written back, overwriting the correct post-eviction values. The result is that `total_tx_size` and `total_tx_cycles` are permanently inflated by the sizes/cycles of every evicted entry.

---

### Finding Description

The vulnerable sequence in `add_entry` is:

```
// Step 1 – snapshot new totals BEFORE any eviction
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   = self.total_tx_size + entry.size  (stale snapshot)

// Step 2 – may evict N entries; each calls update_stat_for_remove_tx,
//           which correctly decrements self.total_tx_size / self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – OVERWRITES the correctly-updated self.total_tx_size with the
//           stale pre-eviction snapshot, losing all decrements from Step 2
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

`updated_stat_for_add_tx` simply returns `self.total_tx_size + entry.size` and `self.total_tx_cycles + entry.cycles` as local values. [1](#0-0) 

`check_and_record_ancestors` can enter the eviction branch when the incoming transaction's ancestor count exceeds `max_ancestors_count` but the excess is attributable to cell-dep parents that can be removed. It calls `remove_entry_and_descendants`, which calls `update_stat_for_remove_tx`, which **modifies `self.total_tx_size` and `self.total_tx_cycles` in-place**. [2](#0-1) 

After `check_and_record_ancestors` returns, the stale local variables are written back unconditionally: [3](#0-2) 

```rust
self.total_tx_size  = total_tx_size;   // stale: does not reflect evictions
self.total_tx_cycles = total_tx_cycles;
```

Each evicted entry's `size` and `cycles` are thus **never subtracted** from the running totals. The pool's accounting is permanently inflated by the aggregate size and cycles of all evicted entries. [4](#0-3) 

---

### Impact Explanation

`total_tx_size` is the primary guard used to enforce the pool's `max_tx_pool_size` limit. When it is inflated, the pool reports itself as fuller than it actually is. Subsequent legitimate transactions are rejected with `Reject::Full` even though real capacity exists. [5](#0-4) 

`total_tx_cycles` is similarly used for cycle-limit enforcement. Both values are exposed via the `tx_pool_info` RPC and drive block-assembly decisions. [6](#0-5) 

The concrete impact is **permanent, cumulative inflation of pool accounting** after each eviction-triggering insertion, causing the pool to prematurely reject valid transactions submitted by any unprivileged user.

---

### Likelihood Explanation

The eviction path is reachable whenever a submitted transaction has enough cell-dep ancestors to exceed `max_ancestors_count` (default 125), but the excess is covered by removable cell-ref parents. An unprivileged tx-pool submitter can deliberately construct such a transaction chain: first flood the pool with a long cell-dep chain, then submit a transaction that references those cells as deps, triggering the eviction. Each such submission permanently inflates the counters. Repeated submissions compound the error until the pool refuses all new transactions. [7](#0-6) 

---

### Recommendation

Move the stat snapshot **after** `check_and_record_ancestors` completes, so it reflects the post-eviction state:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and apply totals only after all evictions are done
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, drop the local-variable pattern entirely and apply the increment directly to `self.total_tx_size`/`self.total_tx_cycles` after all mutations are complete.

---

### Proof of Concept

1. Fill the pool with a chain of 125 transactions `T1 → T2 → … → T125`, where each `Ti` uses the output of `T(i-1)` as a cell dep (not an input), so they all appear as `cell_ref_parents`.
2. Submit a new transaction `T_new` that also references `T1`'s output as a cell dep. Its ancestor count is 126, exceeding `max_ancestors_count = 125`.
3. `check_and_record_ancestors` enters the eviction branch (line 603), evicts `T125` (and its descendants), calling `update_stat_for_remove_tx(T125.size, T125.cycles)` which decrements `self.total_tx_size` correctly.
4. `add_entry` then writes back the stale `total_tx_size` (computed before the eviction), re-inflating it by `T125.size`.
5. Repeat steps 1–4 many times. `total_tx_size` grows without bound relative to the actual pool contents.
6. Eventually `total_tx_size >= max_tx_pool_size` even though the pool holds far fewer bytes, and all subsequent `send_transaction` RPC calls are rejected with `Reject::Full`. [8](#0-7) [2](#0-1)

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

**File:** util/types/src/core/tx_pool.rs (L336-338)
```rust
    pub total_tx_size: usize,
    /// Total consumed VM cycles of all the transactions in the pool.
    pub total_tx_cycles: Cycle,
```
