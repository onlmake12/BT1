Audit Report

## Title
Inflated Descendant-Weight Accounting in `remove_entry_and_descendants` Corrupts Tx-Pool Eviction Ordering — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` pre-strips all link records for the entire subtree before calling `remove_entry` on each node. Because `update_ancestors_index_key` resolves surviving ancestors via those same link records, it finds an empty ancestor set and never invokes `sub_descendant_weight` on any surviving ancestor. Every ancestor of an evicted subtree permanently retains inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` values, corrupting the `EvictKey` used to select which transaction to drop when the pool is full.

## Finding Description
`remove_entry_and_descendants` (lines 252–265) collects the subtree, calls `remove_entry_links` for every node in the batch, then calls `remove_entry` for each: [1](#0-0) 

`remove_entry_links` (lines 418–430) removes the node from `self.links` entirely, and also removes it from its parents' children sets and its children's parents sets: [2](#0-1) 

`remove_entry` (lines 235–250) then calls `update_ancestors_index_key` at line 242: [3](#0-2) 

`update_ancestors_index_key` (lines 432–445) resolves ancestors through `self.links.calc_ancestors`: [4](#0-3) 

`calc_ancestors` (links.rs line 78) calls `calc_relative_ids`, which calls `self.inner.get(short_id)`: [5](#0-4) 

Since `remove_entry_links` already called `self.links.remove(id)` for every node in the batch, the lookup returns `None`, `direct` is empty, and the traversal returns an empty `HashSet`. The `sub_descendant_weight` call on surviving ancestors is therefore never made.

Concrete trace for chain tx1→tx2→tx3, calling `remove_entry_and_descendants(tx2)`:
1. `removed_ids = [tx2, tx3]`
2. `remove_entry_links(tx2)`: removes tx2 from `self.links`, removes tx2 from tx1's children set, removes tx2 from tx3's parents set
3. `remove_entry_links(tx3)`: removes tx3 from `self.links` (parents set is already empty)
4. `self.links` now contains only tx1 (with empty children)
5. `remove_entry(tx2)` → `update_ancestors_index_key(tx2, Remove)` → `calc_ancestors(tx2)` → `self.inner.get(tx2)` returns `None` → empty set → tx1 never updated
6. `remove_entry(tx3)` → same → tx1 never updated

tx1's `descendants_count` remains 3 instead of 1; `descendants_size`, `descendants_cycles`, `descendants_fee` remain inflated by tx2+tx3's values.

The existing test `test_remove_entry_and_descendants` (score_key.rs lines 170–230) confirms the gap: it asserts only that tx2 and tx3 are absent from the pool and from `calc_descendants`, but never asserts that tx1's `descendants_count` returns to 1: [6](#0-5) 

## Impact Explanation
The `descendants_*` fields feed directly into `EvictKey` (entry.rs lines 234–247): [7](#0-6) 

`next_evict_entry` (pool_map.rs lines 380–385) iterates by `evict_key` to select the lowest-priority transaction to drop: [8](#0-7) 

With inflated `descendants_fee` and `descendants_count`, an ancestor whose subtree was already evicted appears to have more and higher-fee descendants than it actually does, raising its apparent `fee_rate` in the eviction key. This makes it look more valuable than it is, causing it to be skipped during eviction. As a result, the pool drops the wrong entry when full — a genuinely low-fee orphaned ancestor is retained while a legitimate high-fee transaction is rejected. This matches **Low (501–2000 points) — any other important performance improvements for CKB**, specifically incorrect tx-pool eviction ordering that degrades pool quality and fee-estimation correctness.

## Likelihood Explanation
Any unprivileged RPC caller or relay peer can trigger this path with four ordinary transactions:
1. Submit parent tx_A via `send_transaction`
2. Submit child tx_B spending an output of tx_A
3. Submit grandchild tx_C spending an output of tx_B
4. Submit conflicting tx_D spending the same input as tx_B

Step 4 causes `resolve_conflict` → `remove_entry_and_descendants(tx_B)`, removing tx_B and tx_C while leaving tx_A with permanently inflated `descendants_*`. No special privilege is required. The inflation accumulates over the pool's lifetime because every call to `resolve_conflict`, `resolve_conflict_header_dep`, and `check_and_record_ancestors` routes through `remove_entry_and_descendants`: [9](#0-8) 

## Recommendation
Capture the set of surviving ancestors of the subtree root **before** any links are torn down, then explicitly decrement their descendant weights after removal:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Capture ancestors of the root BEFORE links are torn down
    let root_ancestors = self.links.calc_ancestors(id);

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Decrement descendants_* for every surviving ancestor
    for removed_entry in &removed {
        for anc_id in &root_ancestors {
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
Add the following assertions to the existing `test_remove_entry_and_descendants` test in `tx-pool/src/component/tests/score_key.rs` after line 229:

```rust
// After map.remove_entry_and_descendants(&tx2_id):
let tx1_entry = map.get(&tx1_id).unwrap();
assert_eq!(tx1_entry.descendants_count, 1); // FAILS: actual value is 3
assert_eq!(tx1_entry.descendants_size, tx1_entry.size); // FAILS: still includes tx2+tx3 sizes
assert_eq!(tx1_entry.descendants_fee, Capacity::shannons(100)); // FAILS: still includes tx2+tx3 fees
```

These assertions fail against the current code, demonstrating the stale accounting.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-243)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
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

**File:** tx-pool/src/component/tests/score_key.rs (L224-229)
```rust
    map.remove_entry_and_descendants(&tx2_id);
    assert!(!map.contains_key(&tx2_id));
    assert!(!map.contains_key(&tx3_id));
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(!descendants_set.contains(&tx2_id));
    assert!(!descendants_set.contains(&tx3_id));
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
