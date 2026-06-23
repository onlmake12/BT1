### Title
`total_tx_size` / `total_tx_cycles` Inflated After Ancestor-Eviction in `add_entry` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the aggregate accounting variables `total_tx_size` and `total_tx_cycles` are computed from a pre-eviction snapshot and then unconditionally written back **after** `check_and_record_ancestors` has already decremented those same variables for every transaction it evicted. The net effect is that the evicted transactions' sizes and cycles are never subtracted from the running totals, causing `total_tx_size` to be permanently inflated. Because `limit_size` uses `total_tx_size` as its sole eviction trigger, the inflation causes the pool to over-evict valid transactions on every subsequent insertion.

---

### Finding Description

`PoolMap::add_entry` in `tx-pool/src/component/pool_map.rs` follows this sequence:

```
// Step 1 – snapshot pre-eviction totals into LOCAL variables
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
//   = self.total_tx_size + entry.size
//   = self.total_tx_cycles + entry.cycles

// Step 2 – may evict N transactions via remove_entry_and_descendants
//   each removal calls update_stat_for_remove_tx which DECREMENTS
//   self.total_tx_size and self.total_tx_cycles
evicts = self.check_and_record_ancestors(&mut entry)?;

// Step 3 – unconditionally OVERWRITE with the pre-eviction snapshot
self.total_tx_size = total_tx_size;   // ← evictions' decrements are lost
self.total_tx_cycles = total_tx_cycles;
``` [1](#0-0) 

`updated_stat_for_add_tx` only reads `self.total_tx_size` / `self.total_tx_cycles` at call time and returns new values into local bindings. [2](#0-1) 

`check_and_record_ancestors` calls `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`, which mutates `self.total_tx_size` and `self.total_tx_cycles` in place for every evicted transaction. [3](#0-2) [4](#0-3) 

The eviction path inside `check_and_record_ancestors` is reached when a new transaction's ancestor count exceeds `max_ancestors_count` but can be reduced by evicting low-fee `cell_ref_parents`. [5](#0-4) 

After Step 3, `self.total_tx_size` equals `(pre-eviction total) + entry.size`, ignoring the `−size(evicted_tx_i)` terms that `update_stat_for_remove_tx` had already applied. The invariant `total_tx_size == Σ entry.size for all entries` is broken.

---

### Impact Explanation

`limit_size` uses `total_tx_size` as its sole loop condition:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee pending/gap/proposed entry
}
``` [6](#0-5) 

An inflated `total_tx_size` causes `limit_size` to evict additional valid transactions even though the pool is actually within its configured size limit. Each subsequent insertion that triggers the ancestor-eviction path compounds the inflation. Over time, the pool will reject or evict legitimate transactions that should have been accepted, degrading mempool utility for all users of the node.

---

### Likelihood Explanation

The trigger requires:
1. Many in-pool transactions that share a common cell dep (e.g., a popular lock script output used as a code dep).
2. A new transaction that **consumes** that cell dep as an input, making all those transactions its `cell_ref_parents`.
3. The resulting ancestor count exceeds `max_ancestors_count` (default 25) but is reducible by evicting some `cell_ref_parents`.

This is a realistic scenario on mainnet (e.g., a transaction spending a widely-referenced cell). The entry path is the standard `send_transaction` RPC, reachable by any unprivileged tx-pool submitter. No special privileges, keys, or majority hashpower are required.

---

### Recommendation

Compute the local `total_tx_size` / `total_tx_cycles` snapshot **after** `check_and_record_ancestors` completes (so evictions are already reflected in `self.total_tx_size`), then add only the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));
// Add only the new entry's contribution on top of the post-eviction totals
self.total_tx_size = self.total_tx_size.saturating_add(entry.size);
self.total_tx_cycles = self.total_tx_cycles.saturating_add(entry.cycles);
```

Alternatively, move the `updated_stat_for_add_tx` call to after `check_and_record_ancestors` and apply it to the already-updated `self.total_tx_*` fields.

---

### Proof of Concept

**Setup:** Pool has transactions A (size=100), B (size=100), C (size=100) all referencing cell dep `X`. `total_tx_size = 300`. `max_ancestors_count = 3`.

**Attack step:** Submit transaction D (size=50) that **spends** cell dep `X` as an input. D now has 3 `cell_ref_parents` (A, B, C) → `ancestors_count = 4 > 3`. The code enters the eviction branch and evicts A (size=100):

- `update_stat_for_remove_tx(100, ...)` → `self.total_tx_size = 200`
- After eviction, `check_and_record_ancestors` returns.
- Step 3 writes `self.total_tx_size = total_tx_size = 300 + 50 = 350`.

**Result:** Pool contains B, C, D with actual total size `100+100+50 = 250`, but `total_tx_size = 350`. The inflation is `100` (the size of the evicted transaction A).

**Consequence:** `limit_size` now sees `350 > max_tx_pool_size` (if configured at e.g. 300) and evicts B or C unnecessarily, even though the pool is actually within limits. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/pool.rs (L298-328)
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
        self.pool_map.entries.shrink_to_fit();
        ret
```
