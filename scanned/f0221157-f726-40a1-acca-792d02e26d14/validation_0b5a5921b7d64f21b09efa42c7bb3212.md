Audit Report

## Title
`remove_entry_and_descendants` Skips Ancestor `descendants_fee/size/cycles` Decrement, Corrupting Eviction-Priority Accounting — (`tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` severs all parent/child links for every entry in the removed subtree before invoking `remove_entry` on each one. Because `update_ancestors_index_key` resolves ancestors through those same links, the call becomes a no-op for every removed entry, and surviving ancestors never have their `descendants_fee`, `descendants_size`, or `descendants_cycles` decremented. In the `remove_by_detached_proposal` path the same entries are immediately re-inserted, causing each surviving ancestor's `descendants_fee` to be counted twice per reorg cycle.

## Finding Description

**Root cause — `remove_entry_and_descendants`**

`remove_entry_and_descendants` first iterates over all entries in the removed subtree and calls `remove_entry_links` on each: [1](#0-0) 

`remove_entry_links` removes the entry from its parents' children sets, removes the entry from its children's parents sets, and then removes the entry's own node from `self.links.inner`: [2](#0-1) 

After this loop, `remove_entry` is called for each id. Inside `remove_entry`, `update_ancestors_index_key` is invoked with `EntryOp::Remove`: [3](#0-2) 

`update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors(...)`: [4](#0-3) 

`calc_ancestors` calls `calc_relative_ids` which looks up `self.inner.get(short_id)`. Because `remove_entry_links` already removed the entry from `self.links.inner`, the lookup returns `None` and the ancestor set is empty. The `sub_descendant_weight` call that should decrement `descendants_fee/size/cycles` on surviving ancestors is never reached. [5](#0-4) 

**Contrast with the single-entry path**

When `remove_entry` is called directly (not via `remove_entry_and_descendants`), links are still intact at the time `update_ancestors_index_key` runs, so ancestors are found and correctly updated. The bug is exclusive to the bulk-removal path.

**Double-counting in `remove_by_detached_proposal`**

`remove_by_detached_proposal` calls `remove_entry_and_descendants` and then immediately re-inserts every removed entry via `add_pending`: [6](#0-5) 

`add_pending` → `add_entry` → `record_entry_descendants` → `update_ancestors_index_key(entry, EntryOp::Add)` increments the surviving ancestor's `descendants_fee/size/cycles` again: [7](#0-6) 

Because the decrement never happened, the ancestor's `descendants_fee` ends up counting the re-inserted subtree twice per reorg cycle. With `saturating_add`, this accumulates until the value saturates at `u64::MAX`.

**Effect on `EvictKey`**

`EvictKey` is computed directly from `descendants_fee`, `descendants_size`, and `descendants_cycles`: [8](#0-7) 

`next_evict_entry` selects the transaction with the lowest `EvictKey` to drop when the pool is full: [9](#0-8) 

An ancestor whose `descendants_fee` is artificially inflated appears to have high-value descendants and is therefore protected from eviction longer than it deserves.

## Impact Explanation

This is a **Medium** severity issue: suboptimal/incorrect implementation of the CKB mempool state storage mechanism (2001–10000 points). The `descendants_fee/size/cycles` fields are permanently inflated on surviving ancestors after any call to `remove_entry_and_descendants`. This corrupts the `EvictKey` index used to decide which transactions to drop when the pool is full. Correctly-priced transactions may be evicted in preference to artificially-boosted low-fee ancestors. Mining priority (`AncestorsScoreSortKey`) is not affected because it uses `ancestors_fee/size/cycles`, not `descendants_*`, so block production is not directly impacted. The impact is confined to mempool eviction ordering.

## Likelihood Explanation

- **Reorg / detached-proposal path**: `remove_by_detached_proposal` is triggered by ordinary block processing during any chain reorganization. No attacker action is required; the double-count accumulates passively on every reorg cycle involving the same pending ancestor.
- **RBF path**: When `min_rbf_rate > min_fee_rate`, any unprivileged RPC caller can submit a replacement transaction, triggering `process_rbf` → `remove_entry_and_descendants`. Two sequential `send_transaction` calls suffice.
- **Conflict-resolution path**: `resolve_conflict` is triggered whenever a new transaction spends an input already consumed by a pool transaction, a normal occurrence.

The reorg path requires no attacker action at all, making this reliably triggered in production.

## Recommendation

Before severing links in `remove_entry_and_descendants`, update the surviving ancestors of the root entry. Specifically, collect the ancestors of the root entry (while links are still intact), compute the total `fee/size/cycles` of the entire removed subtree, and subtract that total from each surviving ancestor's `descendants_*` accumulators. Only then call `remove_entry_links` for each entry in the subtree.

Alternatively, restructure `remove_entry_and_descendants` to call `update_ancestors_index_key` for each removed entry before its links are severed, rather than relying on the post-link-removal call inside `remove_entry`. The comment on line 256 ("update links state for remove, so that we won't update_descendants_index_key in remove_entry") shows the intent was to skip `update_descendants_index_key` (updating children of the removed entry), but this inadvertently also disables `update_ancestors_index_key` (updating parents of the removed entry).

## Proof of Concept

**Scenario A — RBF-triggered inflation**

1. Submit `tx_A` (low fee, e.g. 100 shannons) spending a confirmed cell.
2. Submit `tx_B` (high fee, e.g. 10,000 shannons) spending `tx_A`'s output.
   - `tx_A.descendants_fee` = 10,100 shannons ✓
3. Submit `tx_C` (RBF replacement of `tx_B`, fee = 10,001 shannons, same input as `tx_B`).
   - `process_rbf` calls `remove_entry_and_descendants(tx_B_id)`.
   - Links are removed first; `update_ancestors_index_key(tx_B, Remove)` finds no ancestors → no-op.
   - `tx_A.descendants_fee` remains 10,100 shannons (should be 100 shannons).
4. Pool fills up. `next_evict_entry` iterates by `EvictKey`. `tx_A` appears to have 10,100-shannon descendants and is skipped; a legitimate higher-fee transaction is evicted instead.

**Scenario B — Reorg double-count (no attacker action needed)**

1. `tx_A` (pending), `tx_B` (proposed, child of `tx_A`).
   - `tx_A.descendants_fee` = `tx_A.fee + tx_B.fee`.
2. A 1-block reorg detaches the proposal. `remove_by_detached_proposal({tx_B})` is called.
   - `remove_entry_and_descendants(tx_B)` → links removed → ancestor decrement skipped.
   - `tx_A.descendants_fee` still = `tx_A.fee + tx_B.fee`.
3. `tx_B` is re-inserted via `add_pending` → `record_entry_descendants` → `update_ancestors_index_key(tx_B, Add)`.
   - `tx_A.descendants_fee` += `tx_B.fee` → now = `tx_A.fee + 2 × tx_B.fee`.
4. Each subsequent reorg of the same block doubles the contribution of `tx_B` in `tx_A.descendants_fee`.

A unit test can be written against `PoolMap` directly: add `tx_A` and `tx_B` as a parent-child pair, call `remove_entry_and_descendants` on `tx_B`, and assert that `tx_A.descendants_fee` equals `tx_A.fee` (i.e., no descendants). The test will fail, confirming the bug.

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

**File:** tx-pool/src/pool.rs (L343-353)
```rust
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
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
