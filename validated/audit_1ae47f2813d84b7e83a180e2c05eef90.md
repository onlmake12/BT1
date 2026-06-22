### Title
Stale Ancestor `evict_key` After `remove_entry_and_descendants` Causes Double-Counted Descendant Weight on Re-add — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` pre-removes all link entries before calling `remove_entry` on each. This intentional optimization silently skips the `update_ancestors_index_key` call for every ancestor that **remains** in the pool, leaving those ancestors with inflated `descendants_weight` / `evict_key`. When the same entries are later re-added (e.g., via `remove_by_detached_proposal` during a reorg), `record_entry_descendants` adds the descendant weight a second time, producing a permanently double-counted `evict_key` for the surviving ancestor.

---

### Finding Description

`remove_entry_and_descendants` first strips all link state for every entry in the removal set, then calls `remove_entry` for each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips id from links.inner
    }

    removed_ids
        .iter()
        .filter_map(|