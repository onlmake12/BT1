### Title
`remove_entry_and_descendants` Fails to Update Ancestor `evict_key` After Descendant Removal — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all parent/child links are torn down **before** `remove_entry` is called for each removed transaction. Because `update_ancestors_index_key` relies on those links to locate ancestors, it silently becomes a no-op for every removed entry. Ancestors that remain in the pool are left with stale `evict_key` values that still count the removed descendants, causing the eviction-priority index to be permanently wrong until those ancestors are themselves removed.

---

### Finding Description

`PoolEntry` carries two independent ordered index fields:

- `score: AncestorsScoreSortKey` — used for block-template selection (highest score first)
- `evict_key: EvictKey` — used for pool eviction (lowest key first)

`EvictKey` is derived from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` stored inside `TxEntry`. [1](#0-0) [2](#0-1) 

When a new child is added, `update_ancestors_index_key` walks the link graph to find all ancestors and updates their `descendants_*` fields and `evict_key`: [3](#0-2) 

The symmetric removal path is `remove_entry_and_descendants`. It first strips **all** links for every entry in the removal set, then calls `remove_entry` on each: [4](#0-3) 

`remove_entry` calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`: [5](#0-4) 

But `update_ancestors_index_key` begins by calling `self.links.calc_ancestors(&child.proposal_short_id())`. Because `remove_entry_links` already erased the entry's link record, `calc_ancestors` returns an empty set. The inner loop never executes, and **no ancestor's `evict_key` is updated**. [6](#0-5) 

The ancestors that remain in the pool still carry inflated `descendants_fee` / `descendants_count` values from the now-removed children, so their `evict_key` permanently overstates their effective fee rate.

`remove_entry_and_descendants` is called from two reachable paths:

- `resolve_conflict` — triggered whenever a submitted transaction double-spends an in-pool output
- `resolve_conflict_header_dep` — triggered when a block invalidates a header dep [7](#0-6) 

---

### Impact Explanation

`next_evict_entry` iterates `iter_by_evict_key()` to find the transaction most suitable for eviction when the pool is full: [8](#0-7) 

An ancestor whose `evict_key` still reflects removed high-fee descendants will appear to have a higher effective fee rate than it actually does. It will be ranked **later** in the eviction order than it deserves, preventing it from being evicted. This allows a low-fee transaction to occupy pool space indefinitely, blocking admission of legitimate higher-fee transactions and degrading block-template quality.

---

### Likelihood Explanation

Any unprivileged tx-pool submitter can trigger this:

1. Submit a low-fee transaction **T1** (parent).
2. Submit a high-fee child **T2** spending an output of T1. T1's `evict_key` is updated to reflect T2's fee.
3. Submit a conflicting transaction **T3** that double-spends T2's input. `resolve_conflict` calls `remove_entry_and_descendants` for T2.
4. T2 is removed, but T1's `evict_key` still counts T2's fee. T1 now appears to have a high effective fee rate and is never evicted.
5. Repeat to fill the pool with stale-keyed low-fee ancestors.

No privileged access, no key material, and no majority hash power is required. The attack is cheap (only two transactions per "slot" captured) and fully reversible by the attacker.

---

### Recommendation

Before erasing links, collect the ancestors of the root entry. After all entries are removed, iterate those ancestors and recompute their `descendants_*` fields and `evict_key`. Concretely, in `remove_entry_and_descendants`:

```rust
// Collect ancestors of the root BEFORE links are torn down
let ancestors_to_update: HashSet<ProposalShortId> =
    self.links.calc_ancestors(id);

// ... existing removal logic ...

// After removal, update each surviving ancestor's evict_key
for anc_id in &ancestors_to_update {
    if self.entries.get_by_id(anc_id).is_some() {
        self.entries.modify_by_id(anc_id, |e| {
            // recompute from scratch or subtract removed weights
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

Alternatively, restructure the removal so that links for the root entry are removed **last** (after `update_ancestors_index_key` has already run for it), preserving the existing incremental update logic.

---

### Proof of Concept

```
State before:
  T1 (low fee, pending) → T2 (high fee, pending, child of T1)
  T1.evict_key reflects: descendants_fee = T1.fee + T2.fee  (high)

Attacker submits T3 (conflicts with T2):
  resolve_conflict() → remove_entry_and_descendants(T2)
    remove_entry_links(T2)   ← T1's child link to T2 is removed
    remove_entry(T2):
      update_ancestors_index_key(T2, Remove)
        calc_ancestors(T2) → {} (empty, link already gone)
        → T1's evict_key is NOT updated

State after:
  T1 still in pool
  T1.inner.descendants_fee  = T1.fee + T2.fee  ← stale (T2 is gone)
  T1.inner.descendants_count = 2               ← stale
  T1.evict_key.fee_rate = high                 ← stale

Effect:
  next_evict_entry() ranks T1 as hard-to-evict.
  Pool fills; legitimate high-fee transactions are rejected.
  T1 (low fee) persists indefinitely.
```

### Citations

**File:** tx-pool/src/component/sort_key.rs (L79-84)
```rust
#[derive(Eq, PartialEq, Clone, Debug)]
pub struct EvictKey {
    pub fee_rate: FeeRate,
    pub timestamp: u64,
    pub descendants_count: usize,
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

**File:** tx-pool/src/component/pool_map.rs (L305-331)
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
