### Title
Global `total_tx_size`/`total_tx_cycles` Counters Inflated When Eviction Occurs During `add_entry` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new global `total_tx_size` and `total_tx_cycles` values are computed **before** `check_and_record_ancestors` runs. If that function evicts existing transactions (via `remove_entry_and_descendants`), those evictions correctly decrement `self.total_tx_size`/`self.total_tx_cycles` through `update_stat_for_remove_tx`. However, the pre-eviction computed values are then unconditionally written back to `self`, silently overwriting the decrements. The evicted transactions' sizes and cycles are never subtracted from the global counters, leaving them permanently inflated.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `add_entry` follows this sequence:

```
1. (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
   // Snapshot: new_total = old_total + entry.{size,cycles}

2. evicts = check_and_record_ancestors(&mut entry)
   // May call remove_entry_and_descendants → remove_entry
   //   → update_stat_for_remove_tx(evicted.size, evicted.cycles)
   //   → self.total_tx_{size,cycles} -= evicted.{size,cycles}   ← CORRECT decrement

3. self.total_tx_size  = total_tx_size   // OVERWRITES the decremented value
4. self.total_tx_cycles = total_tx_cycles // OVERWRITES the decremented value
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is triggered when a new transaction's ancestor count exceeds `max_ancestors_count` but some of those ancestors are "cell-ref parents" (transactions sharing the same cell dep). The pool evicts the lowest-fee cell-ref parents to make room. [2](#0-1) 

`remove_entry_and_descendants` calls `remove_entry`, which calls `update_stat_for_remove_tx` and correctly decrements `self.total_tx_size`/`self.total_tx_cycles`: [3](#0-2) [4](#0-3) 

But those decrements are immediately overwritten at lines 218–219: [5](#0-4) 

**Concrete example:**

| Step | `self.total_tx_size` | `self.total_tx_cycles` |
|------|---------------------|----------------------|
| Initial | 1000 | 5000 |
| After `updated_stat_for_add_tx(100, 500)` (local vars) | — | — |
| After eviction of tx(size=200, cycles=1000) | 800 | 4000 |
| After overwrite with local vars (1100, 5500) | **1100** | **5500** |
| **Expected** | **900** | **4500** |

The counters are inflated by the evicted transaction's size (200) and cycles (1000). The developer comment at line 731 — *"cycles overflow is possible, currently obtaining cycles is not accurate"* — acknowledges the known divergence but the fallback (`recompute_total_stat`) only triggers on underflow, not on this inflation path. [6](#0-5) 

---

### Impact Explanation

1. **Incorrect RPC reporting**: `tx_pool_info` returns inflated `total_tx_size` and `total_tx_cycles`, misleading operators and tooling.
2. **Premature pool eviction**: `limit_size` uses `pool_map.total_tx_size` to decide whether to evict transactions. An inflated counter causes valid transactions to be evicted from the pool unnecessarily, degrading mempool quality.
3. **Premature `Reject::Full`**: `updated_stat_for_add_tx` rejects new transactions if `total_tx_cycles` would overflow `u64::MAX`. While unlikely in normal operation, a sufficiently inflated counter (from repeated eviction events) could cause legitimate transactions to be rejected. [7](#0-6) [8](#0-7) 

---

### Likelihood Explanation

The eviction path in `check_and_record_ancestors` requires:
1. A new transaction whose ancestor count exceeds `max_ancestors_count`.
2. Some of those ancestors are "cell-ref parents" — transactions that reference the same cell dep as the new transaction.

An unprivileged tx-pool submitter (via RPC `send_transaction` or P2P relay) can craft a sequence of transactions that all reference the same popular cell dep (e.g., a widely-used lock script cell), forming a chain that exceeds `max_ancestors_count`. Submitting a new transaction into this chain triggers the eviction path. Each such submission inflates the counters by the evicted transaction's size/cycles. Repeated submissions compound the inflation. [9](#0-8) 

---

### Recommendation

Compute `total_tx_size` and `total_tx_cycles` **after** `check_and_record_ancestors` completes (so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles`), then add only the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add new entry's contribution to already-eviction-adjusted counters
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

The pre-check for overflow (`updated_stat_for_add_tx`) should be moved to after eviction, or the evicted sizes/cycles should be subtracted from the pre-computed local variables before writing them back.

---

### Proof of Concept

1. Fill the pool with a chain of N transactions (N = `max_ancestors_count`) that all reference the same cell dep `D`. Each tx has `size = S`, `cycles = C`.
2. Submit a new transaction `T_new` that also references cell dep `D` and spends an output of the chain. Its ancestor count = N+1 > `max_ancestors_count`, but `cell_ref_parents` contains the chain txs, so `ancestors_count - cell_ref_parents.len() <= max_ancestors_count` is satisfied.
3. `check_and_record_ancestors` evicts one chain tx (size=S, cycles=C) via `remove_entry_and_descendants`.
4. `self.total_tx_size` is decremented by S, then overwritten with `old_total + S_new` (ignoring the decrement).
5. Query `tx_pool_info` via RPC: `total_tx_size` is inflated by S compared to the actual sum of entries.
6. Repeat step 2 with new transactions to compound the inflation. [1](#0-0) [10](#0-9)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L60-75)
```rust
pub struct PoolMap {
    /// The pool entries with different kinds of sort strategies
    pub(crate) entries: MultiIndexPoolEntryMap,
    /// All the deps, header_deps, inputs, outputs relationships
    pub(crate) edges: Edges,
    /// All the parent/children relationships
    pub(crate) links: TxLinksMap,
    pub(crate) max_ancestors_count: usize,
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
}
```

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
