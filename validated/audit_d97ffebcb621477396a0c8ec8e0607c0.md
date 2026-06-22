### Title
Ancestor `evict_key` Not Updated When Descendants Are Batch-Removed — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all parent-child links are torn down for every removed entry **before** `remove_entry` is called on any of them. Because `update_ancestors_index_key` relies on those links to locate the surviving ancestors, it silently finds no ancestors and skips the `evict_key` update entirely. The surviving ancestors therefore retain a stale `evict_key` that still reflects the removed descendants' fee-rate and count, corrupting the eviction-priority ordering of the pool — the direct CKB analog of the external report's "node modified without re-shuffling" bug.

---

### Finding Description

`PoolEntry` is stored in a `MultiIndexPoolEntryMap` with two ordered indices:

- `score: AncestorsScoreSortKey` — used for block-template selection (highest score first)
- `evict_key: EvictKey` — used for pool eviction (lowest key first) [1](#0-0) 

`EvictKey` is computed from the entry's **descendant** fee-rate and count: [2](#0-1) 

When a single entry is removed via `remove_entry`, the correct sequence is:

1. Remove from `entries`.
2. Call `update_ancestors_index_key(entry, Remove)` — walks the link graph to find surviving ancestors and recomputes their `evict_key`.
3. Call `update_descendants_index_key(entry, Remove)` — updates descendants' `score`.
4. Call `remove_entry_links` — tears down the link graph. [3](#0-2) 

`update_ancestors_index_key` depends on the link graph being intact: [4](#0-3) 

**The bug** is in `remove_entry_and_descendants`, which inverts this order: it calls `remove_entry_links` for **all** entries in the batch **first**, then calls `remove_entry` for each: [5](#0-4) 

By the time `remove_entry` runs for the root transaction, `self.links.calc_ancestors(root_id)` returns an empty set because the root's link record has already been deleted. The surviving ancestors of the root (which are **not** being removed) never receive the `evict_key` update that would subtract the removed descendants' fee-rate contribution.

The comment acknowledges only half the problem: *"update links state for remove, so that we won't update_descendants_index_key in remove_entry"* — it prevents redundant updates to entries that are themselves being removed, but it also silently suppresses the necessary update to entries that are **not** being removed.

---

### Impact Explanation

After `remove_entry_and_descendants` returns, every surviving ancestor `P` of the removed subtree has a stale `evict_key`:

- `evict_key.fee_rate` still equals `max(descendants_feerate_old, own_feerate)` instead of `own_feerate`.
- `evict_key.descendants_count` is still inflated. [6](#0-5) 

`next_evict_entry` iterates the `evict_key` index in ascending order and picks the first entry matching the requested status: [7](#0-6) 

A stale (inflated) `evict_key` pushes `P` toward the **high** end of the eviction index, making it appear more valuable than it is. When the pool is full, `P` is skipped in favour of other entries that should have been retained. Concretely:

- A low-fee-rate

### Citations

**File:** tx-pool/src/component/pool_map.rs (L46-58)
```rust
#[derive(MultiIndexMap, Clone)]
pub struct PoolEntry {
    #[multi_index(hashed_unique)]
    pub id: ProposalShortId,
    #[multi_index(ordered_non_unique)]
    pub score: AncestorsScoreSortKey,
    #[multi_index(hashed_non_unique)]
    pub status: Status,
    #[multi_index(ordered_non_unique)]
    pub evict_key: EvictKey,
    // other sort key
    pub inner: TxEntry,
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

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
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

**File:** tx-pool/src/component/sort_key.rs (L92-104)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
}
```
