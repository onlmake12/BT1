Audit Report

## Title
Surviving Ancestors' `evict_key` Not Updated After Batch Descendant Removal — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` tears down all link records via `remove_entry_links` for every entry in the batch before calling `remove_entry` on any of them. Because `update_ancestors_index_key` relies on `self.links.calc_ancestors` to locate surviving ancestors, and those link records are already gone, the call silently returns an empty set. Every pool entry that is a parent of the removed subtree root but is not itself being removed retains a stale, inflated `evict_key` — corrupting the eviction-priority ordering of the pool for the lifetime of those entries.

## Finding Description
`remove_entry_and_descendants` (lines 252–265) executes in two phases:

**Phase 1** — for every id in `removed_ids`, call `remove_entry_links(id)`:
```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // deletes id from self.links.inner
}
```
`remove_entry_links` (lines 418–430) calls `self.links.remove(id)` which deletes the entry from `TxLinksMap::inner`.

**Phase 2** — for every id, call `remove_entry(id)`:
```rust
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```
Inside `remove_entry` (lines 235–250), the first index-maintenance call is:
```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```
`update_ancestors_index_key` (lines 432–445) does:
```rust
let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
```
`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)` — but `short_id`'s record was already deleted in Phase 1, so `get` returns `None`, the direct-id set is empty, and the BFS returns `∅`.

The surviving ancestors of the root (entries that are parents of the root but are **not** in `removed_ids`) are never visited. Their `inner.descendants_count`, `inner.descendants_fee`, `inner.descendants_size`, `inner.descendants_cycles`, and derived `evict_key` all remain at the pre-removal values.

The comment `// update links state for remove, so that we won't update_descendants_index_key in remove_entry` correctly explains the intent for avoiding redundant updates to entries that are themselves being removed, but it also inadvertently suppresses the necessary update to entries that are **not** being removed.

## Impact Explanation
`EvictKey` is ordered ascending; `next_evict_entry` picks the first entry matching the requested status:
```rust
self.entries.iter_by_evict_key()
    .find(move |entry| entry.status == status)
```
A stale inflated `evict_key` (carrying a removed descendant's high fee-rate and inflated `descendants_count`) pushes the surviving ancestor toward the **high** end of the eviction index. When the pool is full, that ancestor is skipped in favour of entries that should have been retained. Conversely, legitimate higher-fee-rate entries are evicted prematurely. This constitutes a **suboptimal implementation of the CKB transaction pool state management mechanism** — the pool's eviction ordering is persistently corrupted until the affected ancestor is itself removed.

**Impact: Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism.**

## Likelihood Explanation
Any unprivileged user can trigger this with three ordinary transactions:
1. Submit parent P (low fee rate) — P's `evict_key` is set to its own fee rate.
2. Submit child C of P (high fee rate) — `record_entry_descendants` calls `update_ancestors_index_key(C, Add)`, inflating P's `evict_key`.
3. Submit C′ that double-spends one of C's inputs — `resolve_conflict` calls `remove_entry_and_descendants(&C_id)`, triggering the bug and leaving P's `evict_key` stale.

No special privileges, no victim mistakes, and no external dependencies are required. The condition is reproducible on every CKB node running this code.

## Recommendation
Before tearing down link records, compute and apply the ancestor `evict_key` updates for the **root** entry only (surviving ancestors are only reachable through the root's parent links):

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors' evict_key BEFORE tearing down links,
    // using only the root entry's contribution.
    if let Some(root_entry) = self.entries.get_by_id(id) {
        let root_inner = root_entry.inner.clone();
        self.update_ancestors_index_key(&root_inner, EntryOp::Remove);
    }

    // Now safe to tear down all link records.
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```
Alternatively, restructure the loop so that `remove_entry` (with its link-graph-dependent index updates) is called before `remove_entry_links` for each entry, processing in reverse topological order (descendants first, root last).

## Proof of Concept
**Minimal unit test plan** (add to `tx-pool/src/component/pool_map.rs` test module):

1. Build a `PoolMap` with three entries: grandparent G → parent P → child C (each spending the previous tx's output).
2. Assign G and P low fee rates; assign C a high fee rate so that P's `evict_key.fee_rate` is elevated by `add_descendant_weight`.
3. Record the `evict_key` of P before removal.
4. Call `pool_map.remove_entry_and_descendants(&C_id)`.
5. Assert that P's `evict_key` after removal equals `EvictKey::from(&P_entry_with_reset_descendants)` — i.e., `fee_rate` reflects only P's own fee rate and `descendants_count == 1`.
6. Without the fix, the assertion fails because P's `evict_key` still carries C's inflated fee-rate contribution.