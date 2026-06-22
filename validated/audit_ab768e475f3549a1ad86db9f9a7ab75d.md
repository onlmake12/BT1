### Title
`total_tx_size` / `total_tx_cycles` Invariant Broken by Stale Pre-Eviction Overwrite in `add_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::add_entry`, the new aggregate statistics (`total_tx_size`, `total_tx_cycles`) are computed **before** ancestor-eviction may occur, but are written back **after** eviction. Because eviction immediately and correctly decrements the running totals via `update_stat_for_remove_tx`, the final overwrite restores the pre-eviction (inflated) values. This permanently inflates `total_tx_size` by the byte-size of every evicted entry, breaking the invariant `total_tx_size == Σ entry.size` for all entries currently in the pool. The inflated counter causes `limit_size` to evict additional legitimate transactions from the pool, enabling a persistent tx-pool DoS by an unprivileged submitter.

---

### Finding Description

`PoolMap::add_entry` executes the following sequence:

```
Step 1  let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        // Captures: total_tx_size = self.total_tx_size + entry.size
        //           (self.total_tx_size still includes all current entries)

Step 2  evicts = self.check_and_record_ancestors(&mut entry)?;
        // May call remove_entry_and_descendants → remove_entry
        //   → update_stat_for_remove_tx(evicted.size, evicted.cycles)
        // This IMMEDIATELY decrements self.total_tx_size by evicted sizes.

Step 3  self.record_entry_edges(&entry)?;
Step 4  self.insert_entry(&entry, status);
Step 5  self.record_entry_descendants(&entry);
Step 6  self.track_entry_statics(None, Some(status));

Step 7  self.total_tx_size  = total_tx_size;   // ← OVERWRITES with stale value
        self.total_tx_cycles = total_tx_cycles; // ← OVERWRITES with stale value
```

Let `T` = `self.total_tx_size` before the call, `E` = new entry size, `X` = total size of evicted entries.

| Point in time | Correct value | Actual `self.total_tx_size` |
|---|---|---|
| After Step 1 | `T` | `T` (unchanged) |
| After Step 2 evictions | `T − X` | `T − X` (correctly decremented) |
| After Step 7 overwrite | `T − X + E` | `T + E` (stale value restored) |

The pool now reports `total_tx_size = T + E` but the true sum of entry sizes is `T − X + E`. The invariant is broken by `X` bytes (the size of all evicted entries).

The eviction path is `check_and_record_ancestors` → `remove_entry_and_descendants` → `remove_entry` → `update_stat_for_remove_tx`. This path is taken whenever a new transaction's ancestor count exceeds `max_ancestors_count` (default 1 000) but can be reduced to within the limit by evicting "cell-ref-parent" entries (transactions that reference a cell as a dep that the new transaction spends). [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

`limit_size` enforces the pool capacity limit by comparing `total_tx_size` against `max_tx_pool_size` (default 180 MB):

```rust
while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
    // evict lowest-fee entries
}
```

When `total_tx_size` is inflated by `X` bytes, `limit_size` evicts `X` bytes worth of legitimate pending/proposed transactions that would otherwise have remained in the pool. Each attacker-triggered eviction event permanently inflates the counter; the inflation accumulates across multiple such events and persists until the pool is cleared or a `recompute_total_stat` fallback is triggered (which only fires on underflow, i.e., when the pool is nearly empty).

Consequences:
1. **Legitimate transactions are silently dropped** from the pending/proposed pool.
2. **Fee-rate ordering is distorted**: `limit_size` evicts by lowest fee-rate, so the attacker can force out higher-value transactions.
3. **Persistent DoS**: the inflation is not self-correcting under normal operation; repeated triggering keeps the pool artificially "full". [4](#0-3) [5](#0-4) 

---

### Likelihood Explanation

**Attacker-controlled entry path**: any unprivileged peer or RPC caller that can submit transactions to the tx-pool.

**Trigger condition**: the attacker must submit a transaction whose ancestor count in the pool exceeds `max_ancestors_count` (1 000 by default) while at least one ancestor is a "cell-ref-parent" (a pooled transaction that references as a `cell_dep` an output that the new transaction spends as an input). This satisfies the branch at line 603:

```rust
if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
```

**Setup cost**: building a chain of ~1 001 transactions requires paying fees for each, making the attack moderately expensive but well within reach of a motivated adversary. The attacker recovers most of the CKB (minus fees) when the chain is eventually committed or expires. The inflation effect persists after the chain is gone, so a single successful trigger permanently degrades pool accounting until restart. [6](#0-5) 

---

### Recommendation

Move the stat computation to **after** all evictions have completed, so the snapshot reflects the true post-eviction pool state:

```rust
pub(crate) fn add_entry(
    &mut self,
    mut entry: TxEntry,
    status: Status,
) -> Result<(bool, HashSet<TxEntry>), Reject> {
    let tx_short_id = entry.proposal_short_id();
    if self.entries.get_by_id(&tx_short_id).is_some() {
        return Ok((false, Default::default()));
    }
    // Validate that adding would not overflow BEFORE any mutation.
    self.updated_stat_for_add_tx(entry.size, entry.cycles)?;

    let evicts = self.check_and_record_ancestors(&mut entry)?;
    self.record_entry_edges(&entry)?;
    self.insert_entry(&entry, status);
    self.record_entry_descendants(&entry);
    self.track_entry_statics(None, Some(status));

    // Re-compute AFTER evictions so the snapshot is accurate.
    let (total_tx_size, total_tx_cycles) =
        self.updated_stat_for_add_tx_from_current(entry.size, entry.cycles)?;
    self.total_tx_size  = total_tx_size;
    self.total_tx_cycles = total_tx_cycles;

    Ok((true, evicts))
}
```

Alternatively, apply the increment directly to `self.total_tx_size` / `self.total_tx_cycles` at the end (after evictions have already decremented them), rather than capturing a snapshot at the start.

---

### Proof of Concept

1. Fill the pool with a chain of 1 000 transactions `T1 → T2 → … → T1000`, where `T1` also references cell `C` as a `cell_dep`.
2. Submit transaction `T1001` that spends cell `C` as an input and has `T1000` as its parent. Its ancestor count is 1 001 > 1 000; `T1` is a `cell_ref_parent`; the branch at line 603 fires and `T1` (and its descendants) are evicted.
3. During eviction, `update_stat_for_remove_tx` correctly decrements `total_tx_size` by `size(T1)`. But at line 218, `total_tx_size` is overwritten with the pre-eviction snapshot, re-adding `size(T1)`.
4. Call `tx_pool_info` RPC: `total_tx_size` now exceeds the true sum of entry sizes by `size(T1)`.
5. Submit any new transaction: `limit_size` fires and evicts a legitimate transaction that would otherwise have fit, because the pool appears `size(T1)` bytes fuller than it actually is.
6. Repeat steps 1–5 to accumulate inflation and progressively drain the pool of legitimate transactions. [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/component/pool_map.rs (L710-728)
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

**File:** tx-pool/src/pool.rs (L290-328)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
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

**File:** tx-pool/src/service.rs (L1086-1090)
```rust
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
```
