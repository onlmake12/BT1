### Title
Ancestor Descendant-Stats Not Updated in `remove_entry_and_descendants`, Leading to Stale Eviction Keys - (File: tx-pool/src/component/pool_map.rs)

### Summary

In `PoolMap::remove_entry_and_descendants`, all parent/child links are stripped from every entry in the removed subtree **before** `remove_entry` is called on each of them. Because `update_ancestors_index_key` resolves ancestors through those same links, it finds an empty ancestor set and never decrements the `descendants_fee`, `descendants_size`, `descendants_cycles`, or `descendants_count` fields of any transaction that is an ancestor of the removed subtree root but is **not** itself being removed. Those fields are the sole inputs to `EvictKey`, so the surviving ancestor's eviction priority is permanently inflated.

### Finding Description

`remove_entry_and_descendants` first collects the root and all its descendants, then strips every link for every collected entry, and only then calls `remove_entry` on each:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips ALL links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
``` [1](#0-0) 

Inside `remove_entry`, the first thing done is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
``` [2](#0-1) 

`update_ancestors_index_key` resolves ancestors by walking `self.links`:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),
                ...
            };
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
``` [3](#0-2) 

Because `remove_entry_links` already removed the subtree root's entry from `self.links`, `calc_ancestors` returns an empty set. The surviving parent transaction's `sub_descendant_weight` is **never called**, so its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain at their pre-removal values.

By contrast, when `remove_entry` is called in isolation (not through `remove_entry_and_descendants`), the links are still intact at the time `update_ancestors_index_key` runs, so the ancestor's descendant stats are correctly decremented. The asymmetry is exclusive to the batch-removal path.

The stale fields feed directly into `EvictKey`:

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
            descendants_count: entry.descendants_count,
        }
    }
}
``` [4](#0-3) 

A surviving ancestor whose high-fee-rate child was removed will retain the child's fee in `descendants_fee`, making `descendants_feerate` (and therefore `fee_rate` in `EvictKey`) artificially high. The pool evicts entries in ascending `EvictKey` order, so the ancestor is pushed toward the back of the eviction queue even though it now has no descendants and may have a very low intrinsic fee rate.

`remove_entry_and_descendants` is called from every major removal path:

- `limit_size` (pool-full eviction) [5](#0-4) 
- `remove_by_detached_proposal` (reorg handling) [6](#0-5) 
- `remove_tx` (RPC-triggered removal) [7](#0-6) 
- `resolve_conflict_header_dep` (invalid header dep) [8](#0-7) 
- `check_and_record_ancestors` (cell-ref eviction during RBF) [9](#0-8) 

### Impact Explanation

A surviving ancestor transaction retains inflated `descendants_fee` / `descendants_size` / `descendants_count` after its descendants are removed. Its `EvictKey.fee_rate` is therefore higher than its true fee rate, so it is sorted toward the back of the eviction queue. When the pool is full and `limit_size` runs, legitimate high-fee-rate transactions may be evicted in preference to the stale-keyed low-fee-rate ancestor.

In the RBF scenario the inflation compounds: each time a child is replaced, `remove_entry_and_descendants` is called on the old child without decrementing the parent's descendant stats, and then `add_descendant_weight` is called again when the replacement child is inserted. After N replacements the parent's `descendants_fee` is inflated by N × (old child fee), making it effectively immune to eviction regardless of its own fee rate.

### Likelihood Explanation

The bug is triggered by any code path that calls `remove_entry_and_descendants` on a transaction whose parent is still in the pool. This is a normal operational condition: pool-full eviction of a child transaction, a reorg that detaches a child's proposal, or RBF replacement of a child all satisfy it. An unprivileged tx-pool submitter can deliberately engineer this state by submitting a parent–child chain and then triggering child removal through repeated RBF replacements.

### Recommendation

Before stripping links in `remove_entry_and_descendants`, update the ancestors of the subtree root. Concretely, resolve the root entry's ancestors while links are still intact, then call `sub_descendant_weight` on each ancestor for every entry being removed. Alternatively, restructure the function so that `update_ancestors_index_key` for the root is called before any `remove_entry_links` invocation, mirroring the symmetric behavior of the single-entry `remove_entry` path.

### Proof of Concept

1. Submit `tx_parent` (low fee rate, e.g. 1 shannon/byte) spending an on-chain cell.
2. Submit `tx_child` (high fee rate, e.g. 1000 shannons/byte) spending `tx_parent`'s output.
3. At this point `tx_parent.descendants_fee` = `tx_child.fee`, `tx_parent.descendants_count` = 2.
4. Submit `tx_child_v2` (RBF replacement of `tx_child`, slightly higher fee) spending the same input as `tx_child`.
5. `remove_entry_and_descendants(tx_child)` is called. Because links are stripped first, `tx_parent.descendants_fee` is **not** decremented.
6. `tx_child_v2` is inserted; `tx_parent.descendants_fee` is incremented again by `tx_child_v2.fee`.
7. `tx_parent.descendants_fee` is now `tx_child.fee + tx_child_v2.fee` instead of `tx_child_v2.fee`.
8. Repeat steps 4–7 N times. `tx_parent.descendants_fee` grows by N × `tx_child.fee`.
9. `tx_parent.EvictKey.fee_rate` = `max(inflated_descendants_feerate, 1 shannon/byte)` ≈ inflated value, so `tx_parent` is never selected by `next_evict_entry` even when the pool is full, causing legitimate transactions to be evicted in its place.

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

**File:** tx-pool/src/component/pool_map.rs (L284-286)
```rust
        for (blk_hash, id) in ids {
            let entries = self.remove_entry_and_descendants(&id);
            for entry in entries {
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

**File:** tx-pool/src/component/pool_map.rs (L617-621)
```rust
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
```

**File:** tx-pool/src/component/entry.rs (L234-248)
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
}
```

**File:** tx-pool/src/pool.rs (L306-308)
```rust
            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
```

**File:** tx-pool/src/pool.rs (L343-343)
```rust
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
```

**File:** tx-pool/src/pool.rs (L358-360)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
```
