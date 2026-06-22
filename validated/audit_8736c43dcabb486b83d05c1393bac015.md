Now I have all the information needed to make a precise determination. Let me trace the exact execution path.

### Title
Stale Descendant-Weight Fields on Surviving Pool Ancestors After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` pre-strips all `TxLinksMap` entries for the target and its entire descendant set **before** calling `remove_entry` on each node. Because `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, and those links are already gone, any pool transaction that is an ancestor of the removed subtree root but is **not itself** in the removed set never receives `sub_descendant_weight` calls. Its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are permanently inflated, corrupting `EvictKey` ordering and RPC fee-rate estimates.

---

### Finding Description

**Root cause — `remove_entry_and_descendants` (lines 252–265):** [1](#0-0) 

The function first calls `remove_entry_links` for every node in `removed_ids` (the target plus all its descendants). `remove_entry_links` calls `self.links.remove(id)`, which deletes the node's entry from `TxLinksMap.inner` entirely. [2](#0-1) 

Only after all links are stripped does the function iterate and call `remove_entry` on each node.

**Why `update_ancestors_index_key` finds nothing:**

Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`: [3](#0-2) 

`update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`: [4](#0-3) 

`calc_ancestors` walks `TxLinksMap.inner` starting from the node's own entry: [5](#0-4) 

Because the pre-strip phase already called `self.links.remove(id)` for every node in the removed set, `calc_ancestors` returns an empty set for each of them. `sub_descendant_weight` is never called on any surviving ancestor.

**Concrete scenario:**

Pool contains chain **X → A → B → C** (X is a surviving ancestor, not in the removed set). A block commits a transaction that double-spends A's input. `resolve_conflict` calls `remove_entry_and_descendants(A)`: [6](#0-5) 

`removed_ids = [A, B, C]`. All three links are stripped. When `remove_entry(A/B/C)` runs, `calc_ancestors` returns `{}` for each. X's `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles` are never decremented and remain inflated by the combined weight of A, B, and C.

---

### Impact Explanation

X's `EvictKey` is computed from its stale descendant-weight fields: [7](#0-6) 

`next_evict_entry` iterates by `evict_key`: [8](#0-7) 

An inflated `descendants_fee` makes X appear more valuable than it is, so it survives eviction rounds it should lose. Conversely, legitimate high-value transactions may be evicted in its place. The same stale data feeds `estimate_fee_rate`, producing incorrect fee-rate estimates returned via RPC.

---

### Likelihood Explanation

The trigger path is fully unprivileged: submit X → A → B → C via the standard P2P/RPC transaction submission interface, then get a conflicting transaction (double-spending A's input) included in any block. This is a normal, reachable production code path through `resolve_conflict`. No special role, key, or hashpower is required.

---

### Recommendation

In `remove_entry_and_descendants`, update surviving ancestors' descendant-weight fields **before** stripping links. One approach: for each node being removed, call `update_ancestors_index_key(node, EntryOp::Remove)` while links are still intact, then strip the links. Alternatively, collect the set of surviving ancestors (those not in `removed_ids`) before any stripping and explicitly call `sub_descendant_weight` on them for each removed node.

---

### Proof of Concept

The question's stated PoC (chain A→B→C, check B's fields after removal) is incorrect because B is itself removed. The correct PoC requires a surviving ancestor:

1. Build pool with chain **X → A → B → C** (each spending the previous tx's output).
2. Add all four as Pending entries.
3. Record `X.descendants_count` (should be 3), `X.descendants_fee` (sum of A+B+C fees).
4. Call `pool_map.remove_entry_and_descendants(&A_id)`.
5. Assert `pool_map.contains_key(&X_id)` is true (X survives).
6. Assert `X.descendants_count == 0` — **this assertion fails**; the field still reads 3.
7. Assert `X.descendants_fee == 0` — **this assertion fails**; the field retains the inflated sum.

The stale `EvictKey` on X then causes incorrect ordering in every subsequent `next_evict_entry` and `estimate_fee_rate` call.

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

**File:** tx-pool/src/component/pool_map.rs (L305-316)
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
