### Title
Stale Ancestor `evict_key` After `remove_entry_and_descendants` Clears Links Before Calling `remove_entry` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-clears all transaction links before calling `remove_entry` on each transaction. `remove_entry` internally calls `update_ancestors_index_key`, which relies on those same links to find and update the `evict_key` of remaining ancestors. Because the links are already gone, the ancestor update is silently skipped, leaving ancestors in the pool with permanently inflated descendant-weight metadata and a stale `evict_key`.

---

### Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1 — pre-clear all links:** [1](#0-0) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links cleared here
    }
    ...
}
```

**Phase 2 — call `remove_entry` for each id:** [2](#0-1) 

Inside `remove_entry`, `update_ancestors_index_key` is called:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` queries `self.links.calc_ancestors(...)` to find which remaining pool entries need their descendant-weight and `evict_key` updated: [3](#0-2) 

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id()); // ← returns ∅ — links already gone
    for anc_id in &ancestors {   // ← loop never executes
        self.entries.modify_by_id(anc_id, |e| {
            e.inner.sub_descendant_weight(child);
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

`calc_ancestors` walks `self.links.inner`, but the entry for the removed transaction was already deleted by `remove_entry_links` in Phase 1: [4](#0-3) 

The result is that `calc_ancestors` returns an empty set, the `for` loop body never runs, and no ancestor's `evict_key` or descendant-weight fields (`descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`) are decremented.

The comment in the code ("so that we won't update_descendants_index_key in remove_entry") reveals the intent was only to suppress the descendant update, but it inadvertently also suppresses the ancestor update, which is the bug.

---

### Impact Explanation

Any transaction that is a parent of a removed chain retains permanently inflated descendant-weight metadata:

- `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee` are never decremented.
- `evict_key` (computed from these fields) is never refreshed. [5](#0-4) 

`next_evict_entry` sorts by `evict_key` to select the next transaction to drop when the pool is full:

```rust
pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
    self.entries
        .iter_by_evict_key()
        .find(move |entry| entry.status == status)
        .map(|entry| entry.id.clone())
}
```

A transaction with an inflated `evict_key` appears more valuable than it actually is, so it is systematically deprioritized for eviction. Conversely, genuinely high-value transactions may be evicted ahead of it. This corrupts the pool's eviction ordering silently and persistently for the lifetime of the affected ancestor entry.

---

### Likelihood Explanation

This is triggered by any scenario where a transaction chain is partially removed:

1. **Conflict resolution** (`resolve_conflict`): a new transaction spending the same input as an in-pool transaction causes `remove_entry_and_descendants` to be called on the conflicting tx and its descendants, leaving the parent in the pool with a stale `evict_key`. [6](#0-5) 

2. **Header-dep invalidation** (`resolve_conflict_header_dep`): a reorg invalidates a header dep, removing a child chain while leaving parents. [7](#0-6) 

Both paths are reachable by any unprivileged tx-pool submitter. Submitting a chain of two transactions and then submitting a conflicting transaction for the child is a trivially constructable scenario.

---

### Recommendation

`remove_entry_and_descendants` should update ancestor scores **before** clearing links, or should pass the ancestor set explicitly to `remove_entry` so that `update_ancestors_index_key` can still find and update the remaining pool entries. The simplest fix is to call `update_ancestors_index_key` for the root transaction (the one being removed along with its descendants) while links are still intact, before the pre-clear loop runs.

---

### Proof of Concept

**Setup:** tx1 → tx2 → tx3 (all pending in pool). tx1's `descendants_count = 2`.

**Trigger:** Submit tx4 that spends the same input as tx2. `resolve_conflict` calls `remove_entry_and_descendants(tx2)`.

**Execution trace:**

1. `removed_ids = [tx2, tx3]`
2. `remove_entry_links(tx2)` — removes tx2 from tx1's children, removes tx2's own link entry.
3. `remove_entry_links(tx3)` — removes tx3's link entry.
4. `remove_entry(tx2)` → `update_ancestors_index_key(tx2, Remove)` → `calc_ancestors(tx2)` returns `∅` (tx2's link is gone) → tx1's `evict_key` is **not updated**.
5. `remove_entry(tx3)` → same, no-op.

**Result:** tx1 remains in pool. Its `descendants_count` is still 2, `descendants_size/cycles/fee` still include tx2 and tx3. `evict_key` is permanently stale. When the pool fills up, tx1 is incorrectly ranked as more valuable than it is, preventing correct eviction. [8](#0-7) [3](#0-2) [9](#0-8)

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L267-292)
```rust
    pub(crate) fn resolve_conflict_header_dep(
        &mut self,
        headers: &HashSet<Byte32>,
    ) -> Vec<ConflictEntry> {
        let mut conflicts = Vec::new();

        // invalid header deps
        let mut ids = Vec::new();
        for (tx_id, deps) in self.edges.header_deps.iter() {
            for hash in deps {
                if headers.contains(hash) {
                    ids.push((hash.clone(), tx_id.clone()));
                    break;
                }
            }
        }

        for (blk_hash, id) in ids {
            let entries = self.remove_entry_and_descendants(&id);
            for entry in entries {
                let reject = Reject::Resolve(OutPointError::InvalidHeader(blk_hash.to_owned()));
                conflicts.push((entry, reject));
            }
        }
        conflicts
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

**File:** tx-pool/src/component/links.rs (L37-72)
```rust
    fn calc_relative_ids(
        &self,
        short_id: &ProposalShortId,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let direct = self
            .inner
            .get(short_id)
            .map(|link| link.get_direct_ids(relation))
            .cloned()
            .unwrap_or_default();

        self.calc_relation_ids(direct, relation)
    }

    pub fn calc_relation_ids(
        &self,
        mut stage: HashSet<ProposalShortId>,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let mut relation_ids = HashSet::with_capacity(stage.len());

        while let Some(id) = stage.iter().next().cloned() {
            //recursively
            if let Some(tx_links) = self.inner.get(&id) {
                for direct_id in tx_links.get_direct_ids(relation) {
                    if !relation_ids.contains(direct_id) {
                        stage.insert(direct_id.clone());
                    }
                }
            }
            stage.remove(&id);
            relation_ids.insert(id);
        }
        relation_ids
    }
```
