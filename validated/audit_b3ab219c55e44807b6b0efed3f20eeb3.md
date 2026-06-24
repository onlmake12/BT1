Audit Report

## Title
`remove_entry_and_descendants` Fails to Update Ancestor `evict_key` After Descendant Removal — (`tx-pool/src/component/pool_map.rs`)

## Summary
In `remove_entry_and_descendants`, all link records are erased via `remove_entry_links` for every entry in the removal set before `remove_entry` is called on each. When `remove_entry` subsequently calls `update_ancestors_index_key`, the function calls `calc_ancestors` on the already-unlinked entry, which returns an empty set. As a result, ancestors that remain in the pool are never updated: their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields remain inflated, and their `evict_key` permanently overstates their effective fee rate, breaking the eviction-priority index.

## Finding Description

`remove_entry_and_descendants` pre-removes all link records before calling `remove_entry` on each entry in the removal set: [1](#0-0) 

The comment on line 256 reveals the intent: pre-removing links prevents `update_descendants_index_key` from running inside `remove_entry` for entries that are also being removed. However, this same pre-removal also silences `update_ancestors_index_key` for the root entry's surviving ancestors.

`remove_entry` calls `update_ancestors_index_key` immediately after removing the entry from the index: [2](#0-1) 

`update_ancestors_index_key` begins by calling `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

`calc_ancestors` delegates to `calc_relative_ids`, which does `self.inner.get(short_id)` to find the entry's parent set: [4](#0-3) 

`remove_entry_links` calls `self.links.remove(id)` as its final step, which removes the entry's record from `self.links.inner`: [5](#0-4) [6](#0-5) 

Because `remove_entry_links` was already called for the root entry (e.g., T2), `self.inner.get(T2_id)` returns `None`, `direct` is an empty set, and `calc_relation_ids` returns an empty set. The `for anc_id in &ancestors` loop in `update_ancestors_index_key` never executes. No ancestor's `sub_descendant_weight` is called, and no ancestor's `evict_key` is recomputed.

The stale `evict_key` is derived from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` stored in `TxEntry`: [7](#0-6) 

The `EvictKey` ordering sorts ascending by `fee_rate` first — a higher `fee_rate` means the entry is ranked later (harder to evict): [8](#0-7) 

`next_evict_entry` iterates `iter_by_evict_key()` in ascending order to find the best eviction candidate: [9](#0-8) 

An ancestor whose `evict_key` still reflects a removed high-fee descendant will appear to have a higher effective fee rate than it actually does, ranking it later in eviction order than it deserves.

The two reachable call sites are `resolve_conflict` and `resolve_conflict_header_dep`: [10](#0-9) [11](#0-10) 

## Impact Explanation

The broken eviction index allows low-fee transactions to persist in the pool indefinitely. An attacker can cheaply fill the pool with stale-keyed low-fee ancestors, causing the pool to reject legitimate higher-fee transactions. This degrades block-template quality and can cause CKB network congestion with minimal cost. This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

Any unprivileged submitter can trigger this with two transactions per captured slot:
1. Submit T1 (low fee, parent).
2. Submit T2 (high fee, child of T1). T1's `evict_key` is updated to reflect T2's fee.
3. Submit T3 conflicting with T2. `resolve_conflict` calls `remove_entry_and_descendants(T2_id)`. T2 is removed, but T1's `evict_key` is not updated.
4. T1 remains in the pool with an inflated `evict_key` and is never evicted.
5. Repeat to fill the pool.

No privileged access, no key material, and no majority hash power is required. The attack is cheap and fully repeatable.

## Recommendation

Before erasing any links, collect the ancestors of the root entry. After all entries are removed, iterate those surviving ancestors and recompute their `descendants_*` fields and `evict_key`. Concretely, in `remove_entry_and_descendants`:

```rust
// Collect ancestors of the root BEFORE links are torn down
let ancestors_to_update = self.links.calc_ancestors(id);

// ... existing removal logic (remove_entry_links + remove_entry for all ids) ...

// After removal, update each surviving ancestor's evict_key
for anc_id in &ancestors_to_update {
    if self.entries.get_by_id(anc_id).is_some() {
        for removed_entry in &removed_entries {
            self.entries.modify_by_id(anc_id, |e| {
                e.inner.sub_descendant_weight(removed_entry);
                e.evict_key = e.inner.as_evict_key();
            });
        }
    }
}
```

Alternatively, restructure the removal so that links for the root entry are torn down **last** (after `update_ancestors_index_key` has already run for it), preserving the existing incremental update logic.

## Proof of Concept

```
Initial state:
  T1 (low fee) → T2 (high fee, child of T1)
  T1.inner.descendants_fee   = T1.fee + T2.fee  (high)
  T1.inner.descendants_count = 2
  T1.evict_key.fee_rate      = high  (reflects T2's fee)

Attacker submits T3 (double-spends T2's input):
  resolve_conflict() → remove_entry_and_descendants(T2_id)
    remove_entry_links(T2_id):
      links.remove_child(T1_id, T2_id)   ← T2 removed from T1's children
      links.remove(T2_id)                ← T2's link record deleted
    remove_entry(T2_id):
      update_ancestors_index_key(T2, Remove):
        calc_ancestors(T2_id)
          → links.inner.get(T2_id) = None  (already removed)
          → returns {}
        → loop body never executes
        → T1's evict_key is NOT updated

Final state:
  T1 still in pool
  T1.inner.descendants_fee   = T1.fee + T2.fee  ← stale
  T1.inner.descendants_count = 2                ← stale
  T1.evict_key.fee_rate      = high             ← stale

Effect:
  next_evict_entry() ranks T1 as hard-to-evict.
  Pool fills with stale-keyed low-fee ancestors.
  Legitimate high-fee transactions are rejected.
```

A unit test can confirm this by constructing a `PoolMap` with T1 and T2, calling `remove_entry_and_descendants` for T2, and asserting that T1's `evict_key` equals `T1.inner.as_evict_key()` (i.e., reflects only T1's own fee, not T2's).

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

**File:** tx-pool/src/component/links.rs (L94-96)
```rust
    pub fn remove(&mut self, short_id: &ProposalShortId) -> Option<TxLinks> {
        self.inner.remove(short_id)
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

**File:** tx-pool/src/component/sort_key.rs (L92-104)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
}
```
