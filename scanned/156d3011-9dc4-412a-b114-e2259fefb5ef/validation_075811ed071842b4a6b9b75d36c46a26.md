### Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Score — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records for every entry it is about to delete **before** calling `remove_entry` on each one. Because `update_ancestors_index_key` resolves ancestors through those same link records, it finds an empty ancestor set and never subtracts the removed entries' weight from the `descendants_fee / descendants_size / descendants_cycles / descendants_count` fields of ancestor transactions that remain in the pool. Those ancestors are left with permanently inflated descendant-weight statistics, corrupting the `EvictKey` used to decide which transactions to drop when the pool is full.

---

### Finding Description

`remove_entry_and_descendants` collects the target transaction and all its descendants, strips every link record first, then iterates and calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-264
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

Inside `remove_entry`, the two index-update helpers are called:

```rust
// lines 242-243
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` resolves the ancestor set via `self.links.calc_ancestors(...)`:

```rust
// lines 432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← always empty: links already gone
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

Because every link was removed in the pre-pass, `calc_ancestors` returns an empty set for every entry being processed. No ancestor that **remains** in the pool ever has `sub_descendant_weight` called on it. Its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are never decremented.

By contrast, when `remove_entry` is called **directly** (not through `remove_entry_and_descendants`), links are still intact and the update works correctly, as verified by the existing test:

```rust
// tx-pool/src/component/tests/score_key.rs  lines 157-163
map.remove_entry(&tx1_id);
let tx3_entry = map.get(&tx3_id).unwrap();
assert_eq!(tx3_entry.ancestors_count, 2);   // correctly decremented
```

No equivalent test exists for `remove_entry_and_descendants` checking ancestor descendant-weight after removal.

`remove_entry_and_descendants` is called from three production paths:

| Caller | Trigger |
|---|---|
| `resolve_conflict` | new tx spends the same input as a pooled tx |
| `resolve_conflict_header_dep` | a committed block invalidates a header dep |
| `check_and_record_ancestors` | ancestor-count limit exceeded; cell-ref parents evicted |

All three are reachable by an unprivileged transaction submitter or block relayer.

---

### Impact Explanation

`EvictKey` is computed from the stale fields:

```rust
// tx-pool/src/component/entry.rs  lines 234-247
let descendants_weight = get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey {
    fee_rate: descendants_feerate.max(feerate),
    ...
}
```

An ancestor whose removed descendants had a higher fee rate than itself will retain an inflated `descendants_feerate`, making `fee_rate` appear higher than it truly is. When the pool is full and `next_evict_entry` selects the lowest-`EvictKey` entry to drop, that ancestor is ranked as more valuable than it deserves, so other legitimate transactions are evicted in its place. The stale state persists until the ancestor itself is eventually removed or the pool is cleared.

---

### Likelihood Explanation

The trigger is straightforward: submit a low-fee transaction X, then submit high-fee-rate descendants A → B spending X's output, then submit a conflicting transaction that spends the same input as A. `resolve_conflict` calls `remove_entry_and_descendants(A)`, removing A and B while leaving X with inflated `descendants_fee/size/cycles/count`. The cost is the fee for the conflicting transaction; the benefit is that X resists eviction at the expense of other users' transactions. No privileged access, no majority hash power, and no social engineering are required.

---

### Recommendation

Collect and apply ancestor updates **before** tearing down links. One approach:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update ancestors' descendant-weight BEFORE links are removed,
    // so calc_ancestors() can still resolve them.
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();
    for removed_id in &removed_ids {
        if let Some(entry) = self.get(removed_id).cloned() {
            // Only update ancestors that are NOT themselves being removed.
            let ancestors = self.links.calc_ancestors(removed_id);
            for anc_id in ancestors.difference(&removed_set) {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&entry);
                    e.evict_key = e.inner.as_evict_key();
                });
            }
        }
    }

    // Now safe to strip links (prevents update_descendants_index_key
    // from touching entries that are about to be deleted).
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

Add a regression test that verifies an ancestor's `descendants_count / descendants_fee` are correctly decremented after `remove_entry_and_descendants` is called on a child chain.

---

### Proof of Concept

**Setup**: pool contains chain `X → A → B` (X is the root, B is the leaf).

| Tx | fee | size | cycles |
|---|---|---|---|
| X | 100 | 100 | 100 |
| A | 300 | 200 | 200 |
| B | 200 | 200 | 200 |

After insertion, X's tracked state:
- `descendants_fee = 600`, `descendants_size = 500`, `descendants_cycles = 500`, `descendants_count = 3`

**Trigger**: submit tx A′ that spends the same input as A. `resolve_conflict` calls `remove_entry_and_descendants(A)`.

**Expected state of X after removal**:
- `descendants_fee = 100`, `descendants_size = 100`, `descendants_cycles = 100`, `descendants_count = 1`

**Actual state of X after removal** (bug):
- `descendants_fee = 600`, `descendants_size = 500`, `descendants_cycles = 500`, `descendants_count = 3`

X's `EvictKey.fee_rate` is computed from the inflated `descendants_feerate = 600/500 = 1.2 shannons/KW` instead of the correct `100/100 = 1.0 shannons/KW`. X is ranked as more eviction-resistant than it deserves, and other transactions with a true fee rate between 1.0 and 1.2 are evicted first. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/component/pool_map.rs (L252-264)
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

**File:** tx-pool/src/component/tests/score_key.rs (L157-168)
```rust
    map.remove_entry(&tx1_id);
    assert!(!map.contains_key(&tx1_id));
    assert!(map.contains_key(&tx2_id));
    assert!(map.contains_key(&tx3_id));

    let tx3_entry = map.get(&tx3_id).unwrap();
    assert_eq!(tx3_entry.ancestors_count, 2);
    assert_eq!(
        map.calc_ancestors(&tx3_id),
        vec![tx2_id].into_iter().collect()
    );
}
```
