The code confirms the claim. Let me verify the key mechanics:

1. `remove_entry_and_descendants` (L252-265): first loops over all `removed_ids` calling `remove_entry_links`, then calls `remove_entry` on each.
2. `remove_entry_links` (L418-430): calls `self.links.remove(id)`, fully erasing the entry from `TxLinksMap::inner`.
3. `update_ancestors_index_key` (L432-445): calls `self.links.calc_ancestors(&child.proposal_short_id())`, which in `TxLinksMap` (links.rs L78-80) calls `calc_relative_ids` → `self.inner.get(short_id)` → returns empty when the entry is already removed.
4. `EvictKey` (entry.rs L234-247): computed directly from `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`.

The bug is real and the PoC is mechanically sound.

---

Audit Report

## Title
`descendants_*` Fields Permanently Stale After `remove_entry_and_descendants` Due to Pre-Removal of Links — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` strips all link entries for the removed subtree before calling `remove_entry` on each node. Because `update_ancestors_index_key` discovers ancestors via `self.links.calc_ancestors`, and those link entries are already gone, it returns an empty set and never calls `sub_descendant_weight` on surviving ancestors. The `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` fields of every ancestor that remains in the pool are permanently inflated, corrupting eviction-key ordering for the lifetime of those entries.

## Finding Description
`remove_entry_and_descendants` (L252-265 of `pool_map.rs`) operates in two phases:

**Phase 1 — link teardown:**
```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // erases id from self.links.inner
}
```
`remove_entry_links` (L418-430) calls `self.links.remove(id)`, which removes the `ProposalShortId → TxLinks` entry from `TxLinksMap::inner` entirely.

**Phase 2 — entry removal:**
```rust
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```
`remove_entry` (L235-250) calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`.

`update_ancestors_index_key` (L432-445) does:
```rust
let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
```
`calc_ancestors` (links.rs L78-80) calls `calc_relative_ids`, which starts with `self.inner.get(short_id)`. Because Phase 1 already removed `short_id` from `self.links.inner`, this returns `None`, yielding an empty ancestor set. `sub_descendant_weight` is never called on any surviving ancestor.

The comment at L256 acknowledges the pre-removal is intentional to suppress `update_descendants_index_key` (correct — all descendants are being removed), but it inadvertently also suppresses `update_ancestors_index_key` for ancestors that are **not** being removed.

There is no periodic recomputation of per-entry `descendants_*` fields; `recompute_total_stat` (L698-708) only recomputes pool-wide `total_tx_size`/`total_tx_cycles`.

## Impact Explanation
`EvictKey` is computed from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` (entry.rs L234-247). Entries with a higher `descendants_feerate` are evicted later. An ancestor whose `descendants_fee` is inflated (because removed high-fee descendants were never subtracted) appears to have a higher fee rate than it actually does, so it is deprioritised for eviction. This allows low-fee transactions to occupy pool space beyond their fair share, corrupting the pool's eviction ordering for the remainder of the ancestor's lifetime. This constitutes a **suboptimal implementation of the CKB state storage mechanism** (Medium, 2001–10000 points), as the pool's incremental accounting invariant is permanently broken for affected entries.

## Likelihood Explanation
`remove_entry_and_descendants` is called from `resolve_conflict` (L305-332), `resolve_conflict_header_dep` (L267-292), `limit_size`, and `check_and_record_ancestors` (L618). The `resolve_conflict` path is reachable by any unprivileged user by submitting a double-spend. The attacker submits parent A (low fee), children B and C (high fee), then a conflicting transaction spending B's input. This triggers `resolve_conflict` → `remove_entry_and_descendants(B_id)`, leaving A's `descendants_*` permanently inflated. The attack is cheap, deterministic, and repeatable.

## Recommendation
Before erasing links, collect surviving ancestors and update their descendant accounting:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors BEFORE links are torn down.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid) {
            let inner = entry.inner.clone();
            self.update_ancestors_index_key(&inner, EntryOp::Remove);
        }
    }

    // Now safe to remove links.
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids
        .iter()
        .filter_map(|rid| self.remove_entry_without_ancestor_update(rid))
        .collect()
}
```
Alternatively, add a flag to `remove_entry` to skip `update_ancestors_index_key` and call it explicitly before link removal.

## Proof of Concept
1. Build chain A → B → C (A is parent, C is grandchild), all added to the pool via `add_proposed`.
2. Assert `A.descendants_count == 3`, `A.descendants_fee == fee_A + fee_B + fee_C`.
3. Call `pool_map.remove_entry_and_descendants(&B_id)`.
4. Assert B and C are absent from the pool.
5. Assert `A.descendants_count == 1` and `A.descendants_fee == fee_A` — **this assertion will fail**, demonstrating the stale fields.

This can be written as a unit test in `tx-pool/src/component/tests/` following the pattern of the existing `test_remove_entry` test (score_key.rs L97-168), which already constructs a three-transaction chain and verifies ancestor/descendant accounting after removal.