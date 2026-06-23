### Title
Premature Link Removal in `remove_entry_and_descendants` Causes Stale Descendant-Weight Statistics on Surviving Ancestor Entries - (File: `tx-pool/src/component/pool_map.rs`)

### Summary
In `remove_entry_and_descendants`, all parent/child links are erased for every entry being removed **before** `remove_entry` is called on each of them. Because `update_ancestors_index_key` inside `remove_entry` derives the ancestor set by querying those same links, it always receives an empty set after the pre-removal step. Ancestor entries that remain in the pool are therefore never told to decrement their `descendants_count / descendants_size / descendants_cycles / descendants_fee`, leaving permanently inflated descendant-weight metadata and a corrupted `evict_key` on every surviving ancestor.

### Finding Description

`remove_entry_and_descendants` first collects the target entry and all its descendants, then calls `remove_entry_links` on **every** collected id in a single pass, and only afterwards calls `remove_entry` on each id:

```rust
// tx-pool/src/component/pool_map.rs  L252-L265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← remove_entry called after links are gone
        .collect()
}
```

`remove_entry_links` calls `self.links.remove(id)`, which deletes the entry from `TxLinksMap::inner` and strips cross-references from every parent and child:

```rust
// tx-pool/src/component/pool_map.rs  L418-L430
fn remove_entry_links(&mut self, id: &ProposalShortId) {
    if let Some(parents) = self.links.get_parents(id).cloned() {
        for parent in parents { self.links.remove_child(&parent, id); }
    }
    if let Some(children) = self.links.get_children(id).cloned() {
        for child in children { self.links.remove_parent(&child, id); }
    }
    self.links.remove(id);   // ← entry gone from TxLinksMap
}
```

When `remove_entry` is subsequently called, it invokes `update_ancestors_index_key`:

```rust
// tx-pool/src/component/pool_map.rs  L242-L243
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` queries the link map to find ancestors:

```rust
// tx-pool/src/component/pool_map.rs  L432-L445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← always empty now
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
```

`calc_ancestors` calls `calc_relative_ids`, which starts from `self.inner.get(short_id)`. Because `remove_entry_links` already called `self.links.remove(id)`, `get` returns `None`, the direct-parent set is empty, and the traversal returns an empty `HashSet`. No ancestor entry is ever visited, so `sub_descendant_weight` is never called and `evict_key` is never refreshed on any surviving ancestor. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation

Every surviving ancestor entry accumulates permanently inflated values for `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`. These fields drive two critical pool decisions:

1. **Eviction ordering** — `evict_key` is computed from descendant weight. An ancestor with inflated descendant stats appears more "valuable" than it really is, so it is evicted later than it should be. Conversely, legitimate high-fee transactions with accurate stats may be evicted first.
2. **Fee-rate estimation** — `estimate_fee_rate` iterates pool entries by score; stale descendant-fee values distort the fee-rate curve returned to miners and RPC callers.

The corruption is permanent for the lifetime of the ancestor entry in the pool; it is not corrected when the ancestor is eventually committed or evicted. [4](#0-3) [5](#0-4) 

### Likelihood Explanation

`remove_entry_and_descendants` is called from three production paths, all reachable by an unprivileged peer or transaction sender:

- `resolve_conflict` — triggered whenever a newly submitted transaction spends an output already consumed by a pool transaction (double-spend / RBF). [6](#0-5) 
- `resolve_conflict_header_dep` — triggered when a fork invalidates a header referenced by a pool transaction. [7](#0-6) 
- `check_and_record_ancestors` — triggered during ancestor-limit eviction when a new transaction is added. [8](#0-7) 

A minimal exploit: submit `tx_parent → tx_child` to the pool, then submit a conflicting `tx_conflict` that spends the same input as `tx_child`. `resolve_conflict` calls `remove_entry_and_descendants(tx_child_id)`, removing `tx_child` while leaving `tx_parent` with a `descendants_count` of 1 instead of 0 and a stale `evict_key`.

### Recommendation

Perform the ancestor-weight update **before** tearing down the links. One correct approach is to call `update_ancestors_index_key` for each entry while its links are still intact, and only then call `remove_entry_links`:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // 1. Update ancestor stats while links are still valid
    for id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(id) {
            let inner = entry.inner.clone();
            self.update_ancestors_index_key(&inner, EntryOp::Remove);
        }
    }

    // 2. Now tear down links (prevents redundant descendant updates inside remove_entry)
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

Alternatively, pass a flag to `remove_entry` to skip `update_ancestors_index_key` when the caller has already handled it.

### Proof of Concept

1. Submit `tx_A` (no pool ancestors) to the tx-pool.
2. Submit `tx_B` spending an output of `tx_A`. Pool now has `tx_A.descendants_count == 1`.
3. Submit `tx_C` spending the **same** output of `tx_A` as `tx_B` (conflict). `resolve_conflict` fires, calls `remove_entry_and_descendants(tx_B_id)`.
4. Inside `remove_entry_and_descendants`: `remove_entry_links(tx_B_id)` is called first, removing `tx_B` from `self.links` and removing `tx_B` from `tx_A`'s children set.
5. `remove_entry(tx_B_id)` is then called. `update_ancestors_index_key` calls `calc_ancestors(tx_B_id)` → returns `{}` (empty, because `tx_B` is no longer in `self.links`). `tx_A.sub_descendant_weight(tx_B)` is **never called**.
6. After the operation, `tx_A` remains in the pool with `descendants_count = 1`, `descendants_size`, `descendants_cycles`, `descendants_fee` all inflated, and a stale `evict_key` — permanently, until `tx_A` itself is removed. [1](#0-0) [9](#0-8)

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

**File:** tx-pool/src/component/pool_map.rs (L418-430)
```rust
    fn remove_entry_links(&mut self, id: &ProposalShortId) {
        if let Some(parents) = self.links.get_parents(id).cloned() {
            for parent in parents {
                self.links.remove_child(&parent, id);
            }
        }
        if let Some(children) = self.links.get_children(id).cloned() {
            for child in children {
                self.links.remove_parent(&child, id);
            }
        }
        self.links.remove(id);
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

**File:** tx-pool/src/component/pool_map.rs (L447-460)
```rust
    fn update_descendants_index_key(&mut self, parent: &TxEntry, op: EntryOp) {
        let descendants: HashSet<ProposalShortId> =
            self.links.calc_descendants(&parent.proposal_short_id());
        for desc_id in &descendants {
            // update child score
            self.entries.modify_by_id(desc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_ancestor_weight(parent),
                    EntryOp::Add => e.inner.add_ancestor_weight(parent),
                };
                e.score = e.inner.as_score_key();
            });
        }
    }
```

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
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
