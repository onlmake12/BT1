### Title
`total_tx_size` / `total_tx_cycles` Permanently Inflated After Cell-Dep Ancestor Eviction in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new `total_tx_size` and `total_tx_cycles` values are computed **before** any ancestor-eviction side-effects occur, then written back **after** those evictions have already decremented the same counters. The decrements from evicted transactions are silently overwritten, leaving `total_tx_size` and `total_tx_cycles` permanently inflated by the sizes and cycles of every evicted entry. Because `total_tx_size` is the sole guard for `limit_size()`, the pool will subsequently evict additional legitimate transactions to satisfy a size limit that was never actually exceeded, constituting a Denial-of-Service against the tx-pool.

---

### Finding Description

**Root cause — `add_entry` in `tx-pool/src/component/pool_map.rs`:**

```
Step 1 (line 210-211): pre-eviction snapshot
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
    // total_tx_size  = self.total_tx_size  + entry.size
    // total_tx_cycles = self.total_tx_cycles + entry.cycles

Step 2 (line 213): may evict transactions
    evicts = self.check_and_record_ancestors(&mut entry)?;
    // internally calls remove_entry_and_descendants → remove_entry →
    //   update_stat_for_remove_tx(evicted.size, evicted.cycles)
    // which DECREMENTS self.total_tx_size and self.total_tx_cycles

Step 3 (lines 218-219): overwrites the decrements
    self.total_tx_size  = total_tx_size;   // pre-eviction value restored
    self.total_tx_cycles = total_tx_cycles; // pre-eviction value restored
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is reached when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced to within the limit by removing cell-dep parent transactions: [2](#0-1) 

Each call to `remove_entry_and_descendants` → `remove_entry` calls `update_stat_for_remove_tx`, which correctly decrements `self.total_tx_size` and `self.total_tx_cycles`: [3](#0-2) 

But those decrements are immediately overwritten by the stale pre-eviction snapshot at lines 218-219. The net result is that `total_tx_size` and `total_tx_cycles` are inflated by exactly the sum of sizes and cycles of all evicted transactions.

**Downstream effect — `limit_size` in `tx-pool/src/pool.rs`:**

`limit_size` uses `total_tx_size` as the sole criterion for evicting further transactions:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    ...
    let removed = self.pool_map.remove_entry_and_descendants(&id);
    ...
    callbacks.call_reject(self, &entry, reject);
}
``` [4](#0-3) 

An inflated `total_tx_size` causes `limit_size` to evict additional legitimate transactions that would not have been evicted had the accounting been correct.

---

### Impact Explanation

Every time the cell-dep ancestor eviction path fires inside `add_entry`, `total_tx_size` is over-counted by the aggregate serialized size of the evicted transactions. Subsequent calls to `limit_size` (invoked after every successful `submit_entry`) will then expel legitimate pending/proposed transactions from the pool with `Reject::Full`, even though the pool is not actually full. The inflated counter persists until the pool is cleared or the node is restarted. Repeated triggering accumulates the error, progressively shrinking the effective pool capacity and causing an increasing fraction of honest user transactions to be rejected.

The `total_tx_size` value is also exposed directly via the `tx_pool_info` RPC, so callers receive incorrect pool-size telemetry. [5](#0-4) 

---

### Likelihood Explanation

The eviction path requires a new transaction whose ancestor count (including cell-dep parents) exceeds `max_ancestors_count` (default 1000), but whose non-cell-dep ancestor count is within the limit. An unprivileged tx-pool submitter can construct this scenario by:

1. Submitting a chain of transactions where intermediate transactions are referenced as cell-deps by later transactions.
2. Submitting a final transaction that references one of those intermediate outputs as a cell-dep, pushing the total ancestor count over the limit.

No privileged access, leaked keys, or majority hashpower is required. The attacker only needs the ability to submit transactions to the pool (standard RPC `send_transaction`). The default `max_ancestors_count` of 1000 makes the setup non-trivial but entirely feasible for a motivated attacker, and the effect accumulates across multiple submissions. [6](#0-5) 

---

### Recommendation

Move the `total_tx_size` / `total_tx_cycles` snapshot to **after** `check_and_record_ancestors` completes, so that any eviction-driven decrements are already reflected in `self.total_tx_size` before the new entry's contribution is added:

```rust
// After evictions:
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Now add the new entry's contribution on top of the post-eviction total:
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size  = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, recompute the totals from scratch via `recompute_total_stat()` after all mutations, though this is O(n) in pool size. [7](#0-6) 

---

### Proof of Concept

**Setup (no privileged access required):**

1. Configure a node with `max_ancestors_count = N` (default 1000).
2. Submit a root transaction `tx_root` spending a live cell.
3. Submit `N` child transactions forming a linear chain: `tx_1 → tx_2 → … → tx_N`, each spending the previous output.
4. Submit `tx_dep` that uses `tx_root`'s output as a **cell-dep** (not an input). This makes `tx_root` a `cell_ref_parent` of `tx_dep`.
5. Submit `tx_trigger` that spends `tx_N`'s output AND uses `tx_root`'s output as a cell-dep.

**Trigger:**

- `tx_trigger`'s ancestor count = N (input chain) + 1 (cell-dep `tx_root`) + 1 (self) = N+2 > N.
- `cell_ref_parents = {tx_root}`, so `ancestors_count - cell_ref_parents.len() = N+1 ≤ N+1` — within limit after removing `tx_root`.
- `check_and_record_ancestors` evicts `tx_root` (and `tx_dep` as its descendant) via `remove_entry_and_descendants`, calling `update_stat_for_remove_tx` twice.
- Lines 218-219 then overwrite `self.total_tx_size` with the pre-eviction snapshot, inflating it by `tx_root.size + tx_dep.size`.

**Observable effect:**

- Query `tx_pool_info` via RPC: `total_tx_size` is larger than the sum of sizes of all entries actually in the pool.
- Submit additional transactions: `limit_size` evicts legitimate transactions that fit within the real pool capacity, returning `Reject::Full` to honest submitters. [8](#0-7) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L68-71)
```rust
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
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

**File:** tx-pool/src/component/pool_map.rs (L595-628)
```rust
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
