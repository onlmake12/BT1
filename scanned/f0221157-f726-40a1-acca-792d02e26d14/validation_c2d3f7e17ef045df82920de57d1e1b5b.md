Audit Report

## Title
Inflated Descendant-Weight Accounting in `remove_entry_and_descendants` Corrupts Tx-Pool Eviction Ordering — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` pre-removes all link records for the entire subtree before calling `remove_entry` on each node. Because `update_ancestors_index_key` relies on `self.links.calc_ancestors` to find surviving ancestors, and those links are already gone, the ancestors' `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` fields are never decremented. This permanently inflates the `EvictKey` of every ancestor of any evicted or conflict-resolved subtree, corrupting the eviction ordering used when the pool is full.

## Finding Description
In `remove_entry_and_descendants` (lines 252–265), all link records are stripped first via `remove_entry_links`, then `remove_entry` is called for each node:

```rust
// pool_map.rs lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    for id in &removed_ids {
        self.remove_entry_links(id);   // strips id from self.links entirely
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

`remove_entry_links` (lines 418–430) removes the entry as a child from its parents, removes it as a parent from its children, and then removes it from `self.links` entirely. After this loop, no subtree node has any link record remaining.

`remove_entry` (lines 235–250) then calls `update_ancestors_index_key` (line 242), which calls `self.links.calc_ancestors(&child.proposal_short_id())` (line 433–434). Since `self.links.inner` no longer contains the removed entry, `calc_ancestors` returns an empty `HashSet`. The `for anc_id in &ancestors` loop body never executes, so `sub_descendant_weight` is never called on any surviving ancestor.

For the chain tx1 → tx2 → tx3, calling `remove_entry_and_descendants(tx2)`:
- After the link-stripping loop, tx1's children set is empty and tx2 is gone from `self.links`
- `remove_entry(tx2)` → `calc_ancestors(tx2)` → empty set → tx1's `descendants_count` stays at 3 instead of dropping to 1
- `remove_entry(tx3)` → same result

The comment on line 256 acknowledges the intent to skip `update_descendants_index_key` (updating removed descendants' ancestor weights, which is harmless since those entries are gone), but the same pre-removal of links also silently disables `update_ancestors_index_key` for surviving ancestors — the unintended side effect.

The existing test `test_remove_entry_and_descendants` (lines 170–230 of `score_key.rs`) only asserts that tx2 and tx3 are absent from the pool and from `calc_descendants`, but never asserts that tx1's `descendants_count` returns to 1, leaving the bug undetected.

## Impact Explanation
The inflated `descendants_*` fields feed directly into `EvictKey` (entry.rs lines 234–247): `descendants_feerate` is computed from `descendants_fee` / `descendants_weight`, and `descendants_count` is stored directly. With inflated values, an ancestor whose subtree was already removed appears to have more and higher-fee descendants than it actually does, raising its apparent `fee_rate` in the eviction key.

`next_evict_entry` (pool_map.rs lines 380–385) iterates by `evict_key` ascending to select the lowest-priority transaction to drop. With a falsely elevated `fee_rate`, the ancestor is skipped in favour of genuinely lower-fee transactions. `limit_size` (pool.rs lines 290–328) calls `remove_entry_and_descendants` on the result, compounding the inflation with each eviction cycle.

This constitutes a **suboptimal implementation of the CKB state storage mechanism** (the tx pool is the primary in-memory state store for unconfirmed transactions): the eviction ordering invariant is permanently broken for any ancestor of an evicted subtree, and the corruption accumulates over the lifetime of the pool. It also produces stale `descendants_size`/`descendants_cycles` values in RPC responses (`get_raw_tx_pool`, `get_pool_tx_detail_info`).

**Severity: Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism.**

## Likelihood Explanation
Any unprivileged peer or RPC caller can trigger this with a standard conflict scenario:
1. Submit tx_A (parent).
2. Submit tx_B spending an output of tx_A (child).
3. Submit tx_C spending an output of tx_B (grandchild).
4. Submit tx_D spending the same input as tx_B (conflict).

Step 4 causes `resolve_conflict` → `remove_entry_and_descendants(tx_B)`, leaving tx_A with permanently inflated `descendants_*`. No special privilege is required. The inflation is permanent until tx_A itself is removed, and every subsequent call to `limit_size`, `process_rbf`, `resolve_conflict`, or `resolve_conflict_header_dep` can add further inflation on other ancestors.

## Recommendation
Capture the set of surviving ancestors of the subtree root **before** any links are torn down, then explicitly decrement their descendant weights after removal:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Capture ancestors BEFORE links are removed
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
Add the following assertions to the existing `test_remove_entry_and_descendants` test in `tx-pool/src/component/tests/score_key.rs` after line 224 (`map.remove_entry_and_descendants(&tx2_id)`):

```rust
let tx1_entry = map.get(&tx1_id).unwrap();
assert_eq!(tx1_entry.descendants_count, 1); // FAILS: actual is 3
assert_eq!(tx1_entry.descendants_size, tx1_entry.size); // FAILS: still includes tx2+tx3 sizes
assert_eq!(tx1_entry.descendants_fee, Capacity::shannons(100)); // FAILS: still includes tx2+tx3 fees
```

Running `cargo test test_remove_entry_and_descendants -p ckb-tx-pool` will demonstrate all three assertions fail, confirming the inflated accounting.