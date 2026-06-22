### Title
Global `total_tx_size`/`total_tx_cycles` Not Updated After Eviction During `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the global accounting variables `total_tx_size` and `total_tx_cycles` are pre-computed into local variables before a potential eviction step. When `check_and_record_ancestors` evicts transactions (via `remove_entry_and_descendants`), those evictions correctly subtract from `self.total_tx_size`/`self.total_tx_cycles` — but the final assignment at the end of `add_entry` overwrites those corrected values with the stale pre-eviction snapshot, permanently inflating the global totals.

---

### Finding Description

In `add_entry`:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    ...
    // Step 1: compute new totals into LOCAL variables (old + new entry)
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    // Step 2: may call remove_entry_and_descendants → remove_entry →
    //         update_stat_for_remove_tx, which MODIFIES self.total_tx_size
    //         and self.total_tx_cycles
    evicts = self.check_and_record_ancestors(&mut entry)?;

    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Step 3: OVERWRITES the correctly-modified self.total_tx_size /
    //         self.total_tx_cycles with the stale pre-eviction snapshot
    self.total_tx_size = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;
    Ok((true, evicts))
}
``` [1](#0-0) 

The eviction path inside `check_and_record_ancestors` is:

```rust
while ancestors_count > self.max_ancestors_count {
    if let Some(next_id) = iter.next() {
        let removed = self.remove_entry_and_descendants(next_id);
        ...
    }
}
``` [2](#0-1) 

`remove_entry_and_descendants` calls `remove_entry`, which calls `update_stat_for_remove_tx` and correctly subtracts the evicted entry's `size` and `cycles` from `self.total_tx_size` / `self.total_tx_cycles`: [3](#0-2) [4](#0-3) 

But those correct in-place modifications are then silently discarded when lines 218–219 assign the stale local snapshot back to `self`.

**Concrete example:**

| Step | `self.total_tx_size` | local `total_tx_size` |
|---|---|---|
| Initial | 100 | — |
| After `updated_stat_for_add_tx(+50)` | 100 | **150** |
| After eviction of tx with size 30 | **70** | 150 |
| After `self.total_tx_size = total_tx_size` | **150** ← wrong | — |

Correct value should be `100 − 30 + 50 = 120`. The pool now permanently over-counts by 30 bytes (the evicted tx's size).

---

### Impact Explanation

`total_tx_size` is the sole guard used by `limit_size` to decide when to evict transactions from the pool:

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    ...
    let removed = self.pool_map.remove_entry_and_descendants(&id);
    ...
}
``` [5](#0-4) 

And `updated_stat_for_add_tx` uses it to gate new admissions:

```rust
let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
    Reject::Full(...)
})?;
``` [6](#0-5) 

With an inflated `total_tx_size`:
- The pool believes it is fuller than it actually is.
- `limit_size` over-evicts valid pending/proposed transactions.
- Subsequent legitimate transactions are prematurely rejected with `Reject::Full`.
- The `tx_pool_info` RPC returns incorrect `total_tx_size` and `total_tx_cycles`, misleading operators and fee-estimation logic. [7](#0-6) 

---

### Likelihood Explanation

The eviction branch in `check_and_record_ancestors` fires when:
1. A new transaction's ancestor count (including `cell_ref_parents`) exceeds `max_ancestors_count`.
2. But the count *without* `cell_ref_parents` is within the limit. [8](#0-7) 

An unprivileged tx-pool submitter (RPC caller or relay peer) can craft a chain of transactions that uses cell deps referencing in-pool outputs, then submit a new transaction that triggers this exact branch. No special privilege is required — only the ability to submit transactions to the pool, which is open to any node participant.

---

### Recommendation

Compute the local `total_tx_size`/`total_tx_cycles` snapshot **after** `check_and_record_ancestors` completes (so evictions are already reflected in `self.total_tx_size`/`self.total_tx_cycles`), then add only the new entry's contribution:

```rust
evicts = self.check_and_record_ancestors(&mut entry)?;
self.record_entry_edges(&entry)?;
self.insert_entry(&entry, status);
self.record_entry_descendants(&entry);
self.track_entry_statics(None, Some(status));

// Compute AFTER evictions have already updated self.total_tx_size/cycles
let (total_tx_size, total_tx_cycles) =
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
self.total_tx_size = total_tx_size;
self.total_tx_cycles = total_tx_cycles;
```

Alternatively, move the pre-computation check to a read-only guard before any mutation, and then do a single in-place `+= entry.size` / `+= entry.cycles` after all evictions are done.

---

### Proof of Concept

1. Fill the pool with a chain of transactions `T1 → T2 → … → T_N` where `N = max_ancestors_count`. Ensure `T1` is also referenced as a cell dep by another in-pool transaction `D`.
2. Submit a new transaction `T_new` that spends an output of `T_N` and also references `D`'s output as a cell dep. This makes `D` a `cell_ref_parent`.
3. `ancestors_count = N + 1 > max_ancestors_count`, but `ancestors_count - 1 (cell_ref_parents) = N ≤ max_ancestors_count`, so the eviction branch fires and `D` (and its descendants) are removed via `remove_entry_and_descendants`.
4. After `add_entry` returns, query `tx_pool_info`. `total_tx_size` will be inflated by the size of `D` (and its descendants), even though those entries are no longer in the pool.
5. Repeat step 2 with additional transactions to accumulate inflation until `total_tx_size > max_tx_pool_size`, causing `limit_size` to evict legitimate transactions or causing new submissions to be rejected with `Reject::Full`. [9](#0-8) [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L716-721)
```rust
        let total_tx_size = self.total_tx_size.checked_add(tx_size).ok_or_else(|| {
            Reject::Full(format!(
                "tx-pool total_tx_size {} overflows by add {}",
                self.total_tx_size, tx_size
            ))
        })?;
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

**File:** tx-pool/src/pool.rs (L297-328)
```rust
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

**File:** tx-pool/src/service.rs (L1083-1097)
```rust
        TxPoolInfo {
            tip_hash: tip_header.hash(),
            tip_number: tip_header.number(),
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
        }
```
