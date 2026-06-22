### Title
`remove_entry_and_descendants()` Fails to Update Ancestor Entries' Descendant Weights, Causing Stale Eviction Keys - (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants()` pre-removes all transaction links before calling `remove_entry()` for each entry in the subtree. This causes `update_ancestors_index_key()` inside `remove_entry()` to find no ancestors (links are already gone), so ancestor entries that remain in the pool never have their `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` decremented. By contrast, `remove_entry()` called directly (single-entry removal) correctly updates ancestor descendant weights because links are still intact at the time `update_ancestors_index_key()` runs.

---

### Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, then removes **all** their links in a first pass before calling `remove_entry` for each: [1](#0-0) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links removed here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))  // ← links already gone
        .collect()
}
```

Inside `remove_entry`, `update_ancestors_index_key` is called to decrement the ancestor's descendant weights: [2](#0-1) 

```rust
pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
    self.entries.remove_by_id(id).map(|entry| {
        self.update_ancestors_index_key(&entry.inner, EntryOp::Remove); // ← no-op: links gone
        ...
        self.remove_entry_links(id);   // ← already removed, no-op
        self.track_entry_statics(Some(entry.status), None);
        self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
        entry.inner
    })
}
```

`update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`: [3](#0-2) 

Because all links were pre-removed, `calc_ancestors` returns an empty set. The ancestor entry `A` (which is **not** being removed) never receives `sub_descendant_weight` calls, so its `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee` remain inflated. The `evict_key` of `A` is therefore never refreshed: [4](#0-3) 

Contrast with a direct `remove_entry` call (single-entry removal): links are still intact when `update_ancestors_index_key` runs, so ancestor weights are correctly decremented.

The `EvictKey` is built from these stale descendant fields: [5](#0-4) 

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
            descendants_count: entry.descendants_count,
        }
    }
}
```

`next_evict_entry` iterates by `evict_key` to select the next victim: [6](#0-5) 

A stale (inflated) `evict_key` causes `A` to appear more valuable than it is, so it is evicted later than it should be.

---

### Impact Explanation

After `remove_entry_and_descendants(B)` where `A → B → C` exists in the pool:

- `A` remains in the pool with `descendants_count ≥ 2`, `descendants_fee` including B+C's fees, and `descendants_size`/`descendants_cycles` including B+C's sizes — all stale.
- `A`'s `evict_key` shows an inflated `descendants_feerate`, making it rank as a high-value entry.
- `limit_size` will skip evicting `A` in favor of genuinely high-fee transactions, causing pool bloat with low-fee transactions.
- The stale state persists until `A` is itself removed or the pool is cleared.

---

### Likelihood Explanation

This is triggered by any code path that calls `remove_entry_and_descendants` on a transaction that has an ancestor still in the pool. This occurs in:

- `resolve_conflict` (committed tx conflicts with pool tx that has an in-pool parent)
- `resolve_conflict_header_dep` (header dep invalidated)
- `check_and_record_ancestors` (ancestor count eviction)
- `limit_size` (pool size eviction)
- `remove_tx` (explicit RPC removal)

An unprivileged tx-pool submitter can trigger this by submitting a parent transaction `A` (low fee) followed by child `B` (high fee) and grandchild `C` (high fee), then submitting a conflicting transaction that causes `B` and `C` to be removed via `resolve_conflict`. `A` is left with stale descendant weights.

---

### Recommendation

In `remove_entry_and_descendants`, update the **root entry's** ancestors' descendant weights **before** removing any links. Specifically, call `update_ancestors_index_key(root_entry, EntryOp::Remove)` while links are still intact, then proceed with link removal for the subtree. Alternatively, accumulate the total size/cycles/fee of the removed subtree and apply a single `sub_descendant_weight` update to each ancestor of the root.

---

### Proof of Concept

1. Submit transaction `A` (low fee, e.g. 1 shannon/byte) spending a live cell.
2. Submit transaction `B` (high fee) spending `A`'s output.
3. Submit transaction `C` (high fee) spending `B`'s output.
4. Pool now has `A → B → C`; `A.descendants_count = 3`, `A.descendants_fee = fee_A + fee_B + fee_C`.
5. Submit transaction `D` that spends the same input as `B` (double-spend). `D` gets committed.
6. `remove_committed_tx(D)` → `resolve_conflict(D)` → `remove_entry_and_descendants(B)`.
7. Links for `B` and `C` are removed first; then `remove_entry(B)` and `remove_entry(C)` find no ancestors via `calc_ancestors`, so `A`'s descendant weights are never decremented.
8. `A` now has `descendants_count = 3`, `descendants_fee = fee_A + fee_B + fee_C` despite having zero actual descendants.
9. `A`'s `evict_key` shows the inflated feerate; `limit_size` will not evict `A` even when the pool is full and `A`'s true feerate is the lowest. [1](#0-0) [2](#0-1) [3](#0-2) [6](#0-5) [7](#0-6)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L235-249)
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
```

**File:** tx-pool/src/component/pool_map.rs (L252-264)
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
```

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

**File:** tx-pool/src/component/pool_map.rs (L432-444)
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
