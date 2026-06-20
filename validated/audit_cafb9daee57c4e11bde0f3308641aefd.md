### Title
`total_tx_size`/`total_tx_cycles` Inflated After Eviction in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the updated pool-size statistics (`total_tx_size`, `total_tx_cycles`) are computed **before** any in-pool transactions are evicted, then unconditionally written back **after** the evictions have already decremented those same counters. The evicted transactions' sizes and cycles are therefore never subtracted from the final totals, leaving `total_tx_size` and `total_tx_cycles` permanently inflated relative to the actual pool contents.

---

### Finding Description

`PoolMap::add_entry` follows this sequence:

```
1. (total_tx_size, total_tx_cycles) = updated_stat_for_add_tx(entry.size, entry.cycles)
   // snapshot = old_total + new_entry — computed BEFORE evictions

2. evicts = check_and_record_ancestors(&mut entry)
   // may call remove_entry_and_descendants → remove_entry
   //   → update_stat_for_remove_tx(evicted.size, evicted.cycles)
   //   which correctly DECREMENTS self.total_tx_size / self.total_tx_cycles

3. self.total_tx_size  = total_tx_size   // OVERWRITES the decremented value
   self.total_tx_cycles = total_tx_cycles // with the pre-eviction snapshot
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` fires when a new transaction's ancestor count exceeds `max_ancestors_count` but some ancestors are "cell-ref parents" (pool transactions whose output is referenced as a cell dep by another pool transaction that the new tx will consume as an input). The lowest-fee cell-ref parents are removed via `remove_entry_and_descendants`. [2](#0-1) 

Each call to `remove_entry` correctly calls `update_stat_for_remove_tx`, decrementing `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

But the final assignment at lines 218–219 overwrites those decrements with the stale pre-eviction snapshot, so the evicted transactions' sizes and cycles are never reflected in the totals. [4](#0-3) 

The `update_stat_for_remove_tx` function even has a fallback recompute path for underflow, but that path is never reached here because the overwrite happens *after* the correct decrement, not before.

---

### Impact Explanation

`total_tx_size` is the authoritative counter used to enforce the pool's `max_tx_pool_size` limit and to decide whether to evict existing transactions before accepting a new one. An inflated `total_tx_size` causes the pool to believe it is fuller than it actually is. [5](#0-4) 

Concrete consequences:
- **False pool-full rejections**: Valid transactions submitted by honest users are rejected with a "pool is full" error even though actual pool occupancy is below the limit.
- **Unnecessary evictions**: The pool may evict existing valid transactions to make room that does not need to be made.
- **Incorrect `tx_pool_info` RPC output**: `total_tx_size` and `total_tx_cycles` reported to RPC callers and the terminal are wrong, misleading operators and fee-estimation logic. [6](#0-5) 

The inflation accumulates with each triggering insertion, so repeated submissions can progressively worsen the divergence.

---

### Likelihood Explanation

The eviction path requires a new transaction whose inputs consume an output that is also referenced as a cell dep by another in-pool transaction, pushing the ancestor count over `max_ancestors_count`. This is a non-trivial but fully attacker-controllable condition: any unprivileged transaction sender can submit transactions via the `send_transaction` RPC or P2P relay. No privileged access, key material, or majority hashpower is required. The condition becomes easier to trigger on a busy mempool with long transaction chains.

---

### Recommendation

Compute the updated statistics **after** all evictions have completed, rather than before. Replace the pre-eviction snapshot pattern with a post-eviction read:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Compute and apply the new entry's contribution AFTER evictions are done
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, remove the local snapshot entirely and apply the new entry's delta directly to `self.total_tx_size`/`self.total_tx_cycles` after all mutations, or use `recompute_total_stat` as a post-insertion consistency check.

---

### Proof of Concept

**Setup**: Pool contains transactions A (size=100, cycles=100) and B (size=200, cycles=200), where B references an output of A as a cell dep. `total_tx_size = 300`, `total_tx_cycles = 300`.

**Trigger**: Submit transaction C whose input consumes the same output of A that B uses as a cell dep, and whose ancestor chain length equals `max_ancestors_count`. This causes `check_and_record_ancestors` to evict B (and its descendants) to satisfy the ancestor limit.

**Trace**:
1. `updated_stat_for_add_tx(C.size=50, C.cycles=50)` → snapshot `(350, 350)`.
2. `check_and_record_ancestors` evicts B: `update_stat_for_remove_tx(200, 200)` → `self.total_tx_size = 100`, `self.total_tx_cycles = 100`.
3. C is inserted. `self.total_tx_size = 350`, `self.total_tx_cycles = 350` (snapshot written back).

**Actual pool contents**: A (100) + C (50) = 150 bytes, 150 cycles.
**Reported totals**: 350 bytes, 350 cycles — inflated by B's evicted size/cycles (200 each). [7](#0-6) [8](#0-7)

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

**File:** util/jsonrpc-types/src/pool.rs (L36-39)
```rust
    /// Total size of transactions bytes in the pool of all the different kinds of states (excluding orphan transactions).
    pub total_tx_size: Uint64,
    /// Total consumed VM cycles of all the transactions in the pool (excluding orphan transactions).
    pub total_tx_cycles: Uint64,
```
