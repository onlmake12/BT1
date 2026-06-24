Audit Report

## Title
Stale `descendants_*` Stats on Ancestor Entries After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` pre-strips all link graph entries before calling `remove_entry` on each removed transaction. Because `update_ancestors_index_key` relies on the live link graph to find surviving ancestors, pre-removing the links causes it to return an empty ancestor set — leaving every ancestor of the removed subtree with permanently inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`. An attacker can exploit this via RBF to make a low-fee transaction appear high-value, preventing its eviction and displacing legitimate transactions.

## Finding Description

**Root cause — `remove_entry_and_descendants` (L252-265):**

The function first collects the root id and all its descendants, then calls `remove_entry_links` for every id in that set before calling `remove_entry` on any of them:

```rust
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
for id in &removed_ids {
    self.remove_entry_links(id);
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
``` [1](#0-0) 

`remove_entry_links` (L418-430) removes the entry from its parents' `children` sets, removes it from its children's `parents` sets, and then calls `self.links.remove(id)` — deleting the link entry entirely. [2](#0-1) 

When `remove_entry` is subsequently called for the root (L235-250), it invokes `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` at L242. [3](#0-2) 

`update_ancestors_index_key` (L432-445) calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because the root's link entry was already deleted by the pre-loop, `calc_ancestors` calls `calc_relative_ids` (links.rs L37-50), which does `self.inner.get(short_id)` → returns `None` → `unwrap_or_default()` → empty set. Consequently, `sub_descendant_weight` is **never called** on any surviving ancestor. [4](#0-3) [5](#0-4) 

The comment in the code reveals the intent was only to suppress `update_descendants_index_key` (to avoid updating stats on entries that are themselves being removed), but the pre-removal of links also silently breaks `update_ancestors_index_key` for surviving ancestors. [6](#0-5) 

**Stale fields:** Every ancestor retains inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`. [7](#0-6) 

**Unbounded inflation on re-insertion:** When a new child of the surviving ancestor is later added, `record_entry_descendants` calls `update_ancestors_index_key(new_child, Add)` (pool_map.rs L512), which calls `add_descendant_weight` on the ancestor — stacking new values on top of the already-inflated ones. [8](#0-7) 

**The existing test does not catch this:** `test_remove_entry_and_descendants` (score_key.rs L170-230) only asserts that `calc_descendants` (the live link graph) is correct after removal. It never checks whether tx1's `descendants_count` / `descendants_fee` / etc. were decremented after the call. [9](#0-8) 

## Impact Explanation

The `descendants_*` fields feed directly into `EvictKey` (entry.rs L234-248): `EvictKey::fee_rate` is set to `descendants_feerate.max(feerate)`, where `descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight)`. An ancestor with inflated `descendants_fee` / `descendants_size` will have an artificially high `EvictKey::fee_rate`, making it appear more valuable than it is. [10](#0-9) 

`limit_size` (pool.rs L292-329) uses `next_evict_entry` (ordered by `EvictKey`) to decide what to drop when the pool is full. A low-fee root transaction is never evicted because its `EvictKey` is inflated by the fees of already-removed descendants. Each RBF cycle inflates the ancestor's stats further without bound. Correctly-accounted transactions with genuinely high fee rates are evicted in preference to the attacker's inflated-stat transaction. [11](#0-10) 

This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**. An attacker can occupy pool space indefinitely with a low-fee transaction at minimal cost, displacing legitimate transactions and degrading mempool utility for the network.

## Likelihood Explanation

`remove_entry_and_descendants` is called from six distinct code paths, all reachable by an unprivileged submitter or relayer: `process_rbf` (process.rs L203-206), `resolve_conflict` (pool_map.rs L310, L321), `resolve_conflict_header_dep` (pool_map.rs L285), `remove_by_detached_proposal`, `limit_size`, and `check_and_record_ancestors`. RBF alone is sufficient: submit tx1→tx2, replace tx2 via RBF, and tx1's stats are permanently corrupted. [12](#0-11) [13](#0-12) 

The bug fires whenever the removed entry has at least one ancestor that remains in the pool — a routine condition in any chain of dependent transactions. The attack is cheap, repeatable, and requires no special privileges.

## Recommendation

Before pre-removing links, capture the set of **external ancestors** (ancestors of the root that are not themselves in the removed set) while the link graph is still intact. For each entry in `removed_ids`, call `sub_descendant_weight` on each surviving ancestor. Concretely: compute `calc_ancestors(id)` for the root entry before the pre-removal loop, filter out ids that are themselves in `removed_ids`, and decrement each surviving ancestor's descendant stats for every entry being removed. Alternatively, restructure `remove_entry_and_descendants` to call `update_ancestors_index_key` for the root entry **before** stripping its links, so the normal `remove_entry` path can propagate the decrement correctly to surviving ancestors.

## Proof of Concept

```
State: tx1 (fee=10, size=100) → tx2 (fee=1000, size=100) → tx3 (fee=1000, size=100)
All three are in the pool.

After add_proposed(tx1, tx2, tx3):
  tx1.descendants_count  = 3
  tx1.descendants_fee    = 2010
  tx1.descendants_size   = 300

Call remove_entry_and_descendants(tx2_id):
  Step 1 (pre-loop): remove_entry_links(tx2_id) → tx1's children no longer contains tx2;
                     tx2's link entry deleted.
                     remove_entry_links(tx3_id) → tx3's link entry deleted.
  Step 2: remove_entry(tx2_id):
          update_ancestors_index_key(tx2, Remove):
            calc_ancestors(tx2_id) → {} (link entry gone) → no-op
          → tx1.descendants_* NOT decremented
  Step 3: remove_entry(tx3_id): same — no-op

After removal:
  tx2, tx3 are gone from the pool.
  tx1.descendants_count  = 3   ← should be 1
  tx1.descendants_fee    = 2010 ← should be 10
  tx1.descendants_size   = 300  ← should be 100

tx1's EvictKey now reflects descendants_feerate ≈ 2010/weight(300,…)
instead of feerate = 10/weight(100,…).
tx1 is treated as a high-value transaction and is never evicted.

Repeat: submit tx4 (fee=1000) as child of tx1 → add_descendant_weight adds
1000 to already-inflated tx1.descendants_fee = 3010. Remove tx4 via RBF.
tx1.descendants_fee = 3010 (should be 10). Each cycle inflates further.
```

A minimal regression test would add assertions after `map.remove_entry_and_descendants(&tx2_id)` in `test_remove_entry_and_descendants` (score_key.rs L224-229):
```rust
let tx1_entry = map.get_by_id(&tx1_id).unwrap();
assert_eq!(tx1_entry.inner.descendants_count, 1);
assert_eq!(tx1_entry.inner.descendants_fee, Capacity::shannons(100));
assert_eq!(tx1_entry.inner.descendants_size, 100);
``` [14](#0-13)

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

**File:** tx-pool/src/component/entry.rs (L34-41)
```rust
    /// descendants txs fee
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
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

**File:** tx-pool/src/component/tests/score_key.rs (L170-230)
```rust
#[test]
fn test_remove_entry_and_descendants() {
    let mut map = PoolMap::new(DEFAULT_MAX_ANCESTORS_COUNT);
    let tx1 = TxEntry::dummy_resolve(
        TransactionBuilder::default().build(),
        100,
        Capacity::shannons(100),
        100,
    );
    let tx2 = TxEntry::dummy_resolve(
        TransactionBuilder::default()
            .input(
                CellInput::new_builder()
                    .previous_output(
                        OutPoint::new_builder()
                            .tx_hash(tx1.transaction().hash())
                            .index(0u32)
                            .build(),
                    )
                    .build(),
            )
            .witness(Bytes::new())
            .build(),
        200,
        Capacity::shannons(200),
        200,
    );
    let tx3 = TxEntry::dummy_resolve(
        TransactionBuilder::default()
            .input(
                CellInput::new_builder()
                    .previous_output(
                        OutPoint::new_builder()
                            .tx_hash(tx2.transaction().hash())
                            .index(0u32)
                            .build(),
                    )
                    .build(),
            )
            .witness(Bytes::new())
            .build(),
        200,
        Capacity::shannons(200),
        200,
    );
    let tx1_id = tx1.proposal_short_id();
    let tx2_id = tx2.proposal_short_id();
    let tx3_id = tx3.proposal_short_id();
    map.add_proposed(tx1).unwrap();
    map.add_proposed(tx2).unwrap();
    map.add_proposed(tx3).unwrap();
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(descendants_set.contains(&tx2_id));
    assert!(descendants_set.contains(&tx3_id));
    map.remove_entry_and_descendants(&tx2_id);
    assert!(!map.contains_key(&tx2_id));
    assert!(!map.contains_key(&tx3_id));
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(!descendants_set.contains(&tx2_id));
    assert!(!descendants_set.contains(&tx3_id));
}
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
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
    }
```

**File:** tx-pool/src/process.rs (L203-206)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();
```
