Audit Report

## Title
Stale Descendant Weight Tracking After `remove_entry_and_descendants` Causes Incorrect Pool Eviction Ordering — (File: tx-pool/src/component/pool_map.rs)

## Summary
`PoolMap::remove_entry_and_descendants` pre-strips all link graph entries for every transaction in the removal batch before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors through the now-empty link graph, surviving ancestors of the removed subtree never have their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` decremented. Those ancestors retain permanently inflated `EvictKey` values, causing them to survive pool eviction longer than their true fee rate warrants.

## Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, then calls `remove_entry_links` on every ID in the batch before calling `remove_entry` on each: [1](#0-0) 

`remove_entry_links` removes the entry from its parents' children sets, removes all parents from the entry's own parents set, and deletes the entry's link record entirely: [2](#0-1) 

Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`: [3](#0-2) 

`update_ancestors_index_key` resolves ancestors by calling `self.links.calc_ancestors`, which traverses the link graph starting from the entry's own link record: [4](#0-3) 

`calc_ancestors` calls `calc_relative_ids`, which looks up the entry's link record in `self.inner`. Since `remove_entry_links` already called `self.links.remove(id)` for the root entry, the lookup returns `None`, the initial `direct` set is empty, and the traversal returns an empty `HashSet`: [5](#0-4) 

Consequently, no surviving ancestor ever receives a `sub_descendant_weight` call. The four descendant-weight fields on every surviving ancestor remain at their pre-removal values.

The `EvictKey` is derived directly from these stale fields: [6](#0-5) 

When a new child is subsequently inserted (e.g., the RBF replacement B′), `record_entry_descendants` calls `update_ancestors_index_key(B′, Add)`, adding B′'s weight on top of the already-stale value, compounding the inflation: [7](#0-6) 

`next_evict_entry` selects the entry with the lowest `evict_key` for removal: [8](#0-7) 

Because the ancestor's `evict_key` is inflated, it is never selected for eviction, while legitimate high-fee transactions from other users are dropped instead.

The same staleness occurs in `resolve_conflict`, `resolve_conflict_header_dep`, and the ancestor-count eviction path inside `check_and_record_ancestors`, all of which call `remove_entry_and_descendants`: [9](#0-8) 

## Impact Explanation

The bug causes permanently incorrect `EvictKey` values for surviving ancestors of any removed subtree. When `limit_size` runs during pool pressure, the inflated ancestor survives while legitimate transactions with genuinely higher fee rates are evicted. Each subsequent RBF cycle on the same ancestor compounds the inflation. This constitutes a suboptimal and incorrect implementation of the transaction pool's state tracking mechanism, directly affecting pool admission fairness and eviction ordering — matching **Medium (2001–10000 points): Suboptimal implementation of CKB state storage/pool mechanism**.

## Likelihood Explanation

RBF is enabled by default (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`). Any unprivileged transaction submitter can craft the required chain (A → B → C, then RBF B). The tracking corruption occurs unconditionally on every `remove_entry_and_descendants` call where the removed root has surviving ancestors. The eviction impact materializes whenever the pool is near capacity, which is a normal operating condition on a busy network. No special privileges, keys, or hash power are required.

## Recommendation

Before stripping links in `remove_entry_and_descendants`, capture the surviving ancestors of the root entry. After all removals, explicitly decrement their descendant weights for each removed entry:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Capture surviving ancestors BEFORE links are destroyed
    let surviving_ancestors = self.links.calc_ancestors(id);

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Decrement descendant weights on surviving ancestors
    for removed_entry in &removed {
        for anc_id in &surviving_ancestors {
            self.entries.modify_by_id(anc_id, |e| {
                e.inner.sub_descendant_weight(removed_entry);
                e.evict_key = e.inner.as_evict_key();
            });
        }
    }
    removed
}
```

## Proof of Concept

1. Configure a node with default RBF settings (`min_rbf_rate > min_fee_rate`) and a small `max_tx_pool_size`.
2. Submit tx **A** (fee = 100 shannons).
3. Submit tx **B** spending A's output (fee = 5,000,000 shannons).
4. Submit tx **C** spending B's output (fee = 5,000,000 shannons).
   → A's `descendants_fee` = 10,000,100; A's `EvictKey.fee_rate` is high.
5. Submit tx **B′** conflicting with B, fee > B's fee + RBF surcharge.
   → `process_rbf` calls `remove_entry_and_descendants(B)`, removing B and C.
   → Due to the bug, A's `descendants_fee` remains 10,000,100.
6. B′ is inserted as a new child of A.
   → `update_ancestors_index_key(B′, Add)` adds B′'s fee on top of the stale value.
   → A's `descendants_fee` = 10,000,100 + B′_fee (should be 100 + B′_fee).
7. Fill the pool with many medium-fee transactions until `total_tx_size > max_tx_pool_size`.
8. Observe via `get_pool_tx_detail_info` that A (true fee = 100 shannons) survives eviction while medium-fee transactions from other users are dropped.
9. Repeat steps 5–6 to compound the inflation further across multiple RBF cycles.

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

**File:** tx-pool/src/component/pool_map.rs (L487-513)
```rust
    fn record_entry_descendants(&mut self, entry: &TxEntry) {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let outputs = entry.transaction().output_pts();
        let mut children = HashSet::new();

        // collect children
        for o in outputs {
            if let Some(ids) = self.edges.get_deps_ref(&o).cloned() {
                children.extend(ids);
            }
            if let Some(id) = self.edges.get_input_ref(&o).cloned() {
                children.insert(id);
            }
        }
        // update children
        if !children.is_empty() {
            for child in &children {
                self.links.add_parent(child, tx_short_id.clone());
            }
            if let Some(links) = self.links.inner.get_mut(&tx_short_id) {
                links.children.extend(children);
            }
            self.update_descendants_index_key(entry, EntryOp::Add);
        }
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
    }
```

**File:** tx-pool/src/component/links.rs (L37-50)
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
