### Title
`descendants_*` Accounting of Ancestor Entries Permanently Stale After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all link entries for the removed subtree are torn down **before** `remove_entry` is called on each entry. Because `update_ancestors_index_key` relies on those same links to locate ancestors, it finds an empty ancestor set and never decrements the `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` fields of ancestor entries that remain in the pool. Those fields are permanently inflated for the lifetime of the ancestor entry, corrupting eviction-key ordering and RPC-reported descendant statistics.

---

### Finding Description

`TxEntry` carries two parallel accounting views:

- **Per-entry fields** (`size`, `cycles`, `fee`): the transaction's own weight.
- **Aggregate descendant fields** (`descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`): a running total of all in-pool descendants, maintained incrementally. [1](#0-0) 

These descendant fields are updated via `add_descendant_weight` / `sub_descendant_weight`, called from `update_ancestors_index_key`: [2](#0-1) 

`remove_entry_and_descendants` is the only bulk-removal path. It first strips all link entries for every node in the removed subtree, then calls `remove_entry` on each: [3](#0-2) 

Inside `remove_entry`, `update_ancestors_index_key` is called to notify surviving ancestors that a descendant is gone: [4](#0-3) 

`update_ancestors_index_key` discovers ancestors by calling `self.links.calc_ancestors(&child.proposal_short_id())`. But by the time this runs, the link entry for `child` has already been erased by the earlier `remove_entry_links` loop. `calc_ancestors` therefore returns an empty set, and **no ancestor's `descendants_*` fields are ever decremented**.

The comment in the code acknowledges the link pre-removal is intentional to suppress `update_descendants_index_key` (correct — descendants are being removed anyway), but it inadvertently also suppresses `update_ancestors_index_key` for the surviving ancestors. [5](#0-4) 

There is no periodic recomputation of per-entry `descendants_*` fields. `recompute_total_stat` only recomputes the pool-wide `total_tx_size` / `total_tx_cycles`: [6](#0-5) 

The stale values persist in every surviving ancestor until that ancestor is itself removed.

---

### Impact Explanation

The `EvictKey` for each entry is computed from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`: [7](#0-6) 

Entries with a **higher** `descendants_feerate` are evicted **later**. An ancestor whose `descendants_fee` is inflated (because removed high-fee descendants were never subtracted) appears to have a higher fee rate than it actually does, so it is deprioritised for eviction. This corrupts the pool's eviction ordering and allows low-fee transactions to occupy pool space beyond their fair share.

Additionally, the `TxEntryInfo` returned by the `get_pool_tx_verbose` RPC exposes `descendants_size` and `descendants_cycles` directly: [8](#0-7) 

Callers (wallets, fee estimators, block assemblers) receive permanently incorrect descendant statistics for any ancestor that survived a `remove_entry_and_descendants` call.

---

### Likelihood Explanation

`remove_entry_and_descendants` is invoked from multiple reachable code paths:

- `resolve_conflict` — triggered whenever a newly submitted transaction spends an output already consumed by a pool transaction (standard double-spend / RBF scenario).
- `limit_size` — triggered automatically when the pool exceeds `max_tx_pool_size`.
- `resolve_conflict_header_dep` — triggered on chain reorgs.
- `remove_by_detached_proposal` — triggered on proposal expiry. [9](#0-8) 

Any unprivileged tx-pool submitter can reliably trigger the bug by:

1. Submitting a parent transaction A (low fee).
2. Submitting child B and grandchild C spending A's outputs (high fee).
3. Submitting a conflicting transaction that spends B's input, causing `resolve_conflict` → `remove_entry_and_descendants(B)`.

After step 3, A's `descendants_*` fields remain inflated by B's and C's weights indefinitely.

---

### Recommendation

Before erasing links in `remove_entry_and_descendants`, collect and update the surviving ancestors' descendant fields. One approach:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors' descendant accounting BEFORE links are torn down.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid) {
            let inner = entry.inner.clone();
            self.update_ancestors_index_key(&inner, EntryOp::Remove);
        }
    }

    // Now safe to remove links (descendants' ancestor fields need not be updated
    // since they are all being removed).
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids
        .iter()
        .filter_map(|rid| self.remove_entry_without_ancestor_update(rid))
        .collect()
}
```

Alternatively, refactor `remove_entry` to accept a flag that skips `update_ancestors_index_key` (analogous to the `isPartialRedeem` flag suggested in the Sherlock report), and call the ancestor update explicitly before link removal.

---

### Proof of Concept

Chain: A → B → C (A is parent, C is grandchild). All in pool.

```
A.descendants_count  = 3
A.descendants_fee    = fee_A + fee_B + fee_C
```

Submit a tx that conflicts with B. `resolve_conflict` calls `remove_entry_and_descendants(B_id)`.

`removed_ids = [B_id, C_id]`.

Loop removes links for B and C. A's children list no longer contains B, and B's link entry is gone.

`remove_entry(B_id)` → `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B_id)` → **empty** (B's link entry is gone) → A's `descendants_*` not touched.

`remove_entry(C_id)` → same result.

After removal, B and C are gone from the pool, but:

```
A.descendants_count  = 3   // should be 1
A.descendants_fee    = fee_A + fee_B + fee_C  // should be fee_A
A.descendants_size   = size_A + size_B + size_C  // should be size_A
```

A's `EvictKey` reflects a falsely high `descendants_feerate`, preventing correct eviction ordering for the remainder of A's time in the pool. [3](#0-2) [2](#0-1) [10](#0-9)

### Citations

**File:** tx-pool/src/component/entry.rs (L35-41)
```rust
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
```

**File:** tx-pool/src/component/entry.rs (L133-142)
```rust
    pub fn sub_descendant_weight(&mut self, entry: &TxEntry) {
        self.descendants_count = self.descendants_count.saturating_sub(1);
        self.descendants_size = self.descendants_size.saturating_sub(entry.size);
        self.descendants_cycles = self.descendants_cycles.saturating_sub(entry.cycles);
        self.descendants_fee = Capacity::shannons(
            self.descendants_fee
                .as_u64()
                .saturating_sub(entry.fee.as_u64()),
        );
    }
```

**File:** tx-pool/src/component/entry.rs (L182-194)
```rust
    pub fn to_info(&self) -> TxEntryInfo {
        TxEntryInfo {
            cycles: self.cycles,
            size: self.size as u64,
            fee: self.fee,
            ancestors_size: self.ancestors_size as u64,
            ancestors_cycles: self.ancestors_cycles,
            descendants_size: self.descendants_size as u64,
            descendants_cycles: self.descendants_cycles,
            ancestors_count: self.ancestors_count as u64,
            timestamp: self.timestamp,
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);

        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
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

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
    }
```

**File:** tx-pool/src/component/pool_map.rs (L305-332)
```rust
    pub(crate) fn resolve_conflict(&mut self, tx: &TransactionView) -> Vec<ConflictEntry> {
        let mut conflicts = Vec::new();

        for i in tx.input_pts_iter() {
            if let Some(id) = self.edges.remove_input(&i) {
                let entries = self.remove_entry_and_descendants(&id);
                if !entries.is_empty() {
                    let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                    let rejects = std::iter::repeat_n(reject, entries.len());
                    conflicts.extend(entries.into_iter().zip(rejects));
                }
            }

            // deps consumed
            if let Some(x) = self.edges.remove_deps(&i) {
                for id in x {
                    let entries = self.remove_entry_and_descendants(&id);
                    if !entries.is_empty() {
                        let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                        let rejects = std::iter::repeat_n(reject, entries.len());
                        conflicts.extend(entries.into_iter().zip(rejects));
                    }
                }
            }
        }

        conflicts
    }
```

**File:** tx-pool/src/component/pool_map.rs (L432-445)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
        for anc_id in &ancestors {
            // update parent score
            self.entries.modify_by_id(anc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_descendant_weight(child),
                    EntryOp::Add => e.inner.add_descendant_weight(child),
                };
                e.evict_key = e.inner.as_evict_key();
            });
        }
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
