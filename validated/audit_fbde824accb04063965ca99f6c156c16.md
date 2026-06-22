### Title
Stale `descendants_*` Statistics After `remove_entry_and_descendants` Corrupts Eviction Priority - (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all transaction links are removed **before** `remove_entry` is called for each entry. Because `update_ancestors_index_key` relies on `calc_ancestors` (which traverses links) to find which surviving ancestors need their `descendants_*` stats decremented, removing links first causes those updates to be silently skipped. Ancestors of the removed subtree root retain inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` values permanently. These stale values feed directly into `EvictKey` computation, corrupting eviction priority for the affected entries.

---

### Finding Description

`remove_entry_and_descendants` first strips all links for every entry in the subtree, then calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← links torn down here for ALL entries
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← remove_entry called after links gone
        .collect()
}
```

Inside `remove_entry`, the call to `update_ancestors_index_key` is supposed to walk up to every surviving ancestor and call `sub_descendant_weight` on it:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← returns ∅ because links already removed
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

Because `calc_ancestors` returns an empty set (links are gone), **no ancestor's `descendants_*` fields are ever decremented**. The stale values persist in the pool indefinitely.

`remove_entry_and_descendants` is called from multiple hot paths:
- `resolve_conflict` — triggered on every conflicting `send_transaction` RPC call
- `resolve_conflict_header_dep` — triggered on chain reorganization
- `check_and_record_ancestors` — triggered when a new tx evicts a cell-dep parent

---

### Impact Explanation

The `EvictKey` for a surviving ancestor `A` is computed from its (now stale) `descendants_fee` and `descendants_weight`:

```rust
// tx-pool/src/component/entry.rs
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
        }
    }
}
```

`next_evict_entry` iterates by `evict_key` to select the next victim when the pool is full. With inflated `descendants_fee` and `descendants_weight` from removed children, `descendants_feerate` is computed from phantom data:

- If the removed children had **higher** fee rates than `A`, `A`'s `descendants_feerate` is inflated → `A` appears more valuable → `A` is **not evicted when it should be**, blocking legitimate higher-fee transactions.
- If the removed children had **lower** fee rates than `A`, `A`'s `descendants_feerate` is deflated → `A` appears less valuable → `A` is **evicted unfairly**, even though its own fee rate is sufficient.

---

### Likelihood Explanation

The trigger is any `send_transaction` RPC call that conflicts with an existing in-pool transaction that itself has an in-pool parent. This is a routine, unprivileged operation. An attacker can deliberately engineer the scenario:

1. Submit transaction `A` (low fee rate) to the pool.
2. Submit transaction `B` (high fee rate, spending an output of `A`) to the pool.
3. Submit transaction `C` that spends the same input as `B` (double-spend / RBF attempt).
4. `resolve_conflict` calls `remove_entry_and_descendants(B)`.
5. `B` is removed but `A`'s `descendants_fee` still includes `B`'s fee.
6. `A` now has an inflated `EvictKey` and survives pool eviction rounds it should not survive.

No special privilege, key material, or majority hash power is required.

---

### Recommendation

Before removing links, capture the set of surviving ancestors of the subtree root and update their `descendants_*` stats explicitly. One approach: collect the ancestors of `id` (before any link removal), then after all entries are removed, call `sub_descendant_weight` on each surviving ancestor for every removed entry that was its descendant. Alternatively, restructure `remove_entry_and_descendants` so that `update_ancestors_index_key` for the root entry is called **before** `remove_entry_links` strips the root's parent links, while still suppressing the per-descendant updates (which are unnecessary since all descendants are being removed).

---

### Proof of Concept

Consider pool state: `A → B → C` (A is root, C is leaf, all in `Pending`).

1. `A.descendants_count = 3`, `A.descendants_fee = fee_A + fee_B + fee_C`.
2. Call `remove_entry_and_descendants(B)`.
3. `removed_ids = [B, C]`. Links for B and C are removed first.
4. `remove_entry(B)`: `calc_ancestors(B)` → `∅` (links gone) → `A.descendants_*` unchanged.
5. `remove_entry(C)`: `calc_ancestors(C)` → `∅` → nothing updated.
6. After the call: `A.descendants_count` is still `3`, `A.descendants_fee` still includes `fee_B + fee_C`.
7. `A.evict_key` is recomputed from phantom descendant data → eviction priority is wrong.

The existing test `test_remove_entry` in `tx-pool/src/component/tests/score_key.rs` only checks `ancestors_count` of a surviving descendant after removing a single entry via `remove_entry` (not `remove_entry_and_descendants`), so this path is not covered. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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
