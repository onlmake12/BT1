The code matches the claim exactly. Let me verify the key mechanics:

Audit Report

## Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Surviving Ancestor `EvictKey` Inflated — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` strips all link records in a first pass before calling `remove_entry` on each evicted entry. Because `update_ancestors_index_key` resolves ancestors through those same link records, it finds an empty ancestor set and never calls `sub_descendant_weight` on surviving parents. Any ancestor not in the removed set retains permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, causing its stored `evict_key` to overstate its value and suppressing its eviction priority.

## Finding Description

**Phase 1 — links stripped first** (`pool_map.rs` L252–259):
```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // calls self.links.remove(id) — erases id from links.inner
}
```
`remove_entry_links` terminates with `self.links.remove(id)` (L429), which deletes the entry's record from `self.links.inner`.

**Phase 2 — entries removed** (`pool_map.rs` L261–264):
```rust
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```
Inside `remove_entry` (L242), the first call is:
```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```
`update_ancestors_index_key` (L433–434) does:
```rust
let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
```
`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)` (links.rs L44). Because Phase 1 already removed the entry from `links.inner`, this returns `None`, `unwrap_or_default()` yields an empty `HashSet`, and the `for anc_id in &ancestors` loop (L435) never executes. `sub_descendant_weight` is never called on any surviving ancestor; their `evict_key` is never refreshed.

The single-entry path (`remove_entry` called directly) is correct because it calls `update_ancestors_index_key` **before** `remove_entry_links` (L242 vs L245), so the link record is still present when ancestors are resolved.

The `EvictKey` is computed from the stale `descendants_fee`/`descendants_size`/`descendants_cycles` (entry.rs L236–246), and `next_evict_entry` selects the eviction candidate by iterating `iter_by_evict_key` (pool_map.rs L380–384). A surviving ancestor with inflated stats is ranked as more valuable than it actually is and is skipped during eviction.

## Impact Explanation
This is a suboptimal implementation of the CKB transaction-pool state storage mechanism (Medium, 2001–10000 points). The pool's eviction ordering is corrupted: a surviving ancestor with stale `descendants_feerate` will be ranked above legitimate higher-fee-rate transactions and survive eviction rounds it should not. Honest users' transactions may be displaced from the pool in its place. The corruption persists for the lifetime of the surviving ancestor in the pool (until confirmed or eventually evicted by other means).

## Likelihood Explanation
The trigger path is `resolve_conflict → remove_entry_and_descendants`, reachable by any unprivileged RPC caller or P2P relay peer whenever a submitted transaction conflicts with an existing pool entry. The concrete three-step sequence (submit `tx_A`, submit child `tx_B`, submit RBF replacement `tx_B'`) requires only that `min_rbf_rate > min_fee_rate` (a common deployment configuration). No privileged access, victim mistake, or external dependency is required. The bug is deterministic and repeatable.

## Recommendation
Before stripping links, iterate over each entry being removed, resolve its surviving ancestors (those not in `removed_ids`) while the link records are still intact, and call `sub_descendant_weight` / refresh `evict_key` on each surviving ancestor explicitly. Only then proceed with `remove_entry_links` and `remove_entry`. This mirrors the correct ordering already present in the single-entry `remove_entry` path.

## Proof of Concept
1. Insert `tx_A` (low fee-rate, size=200) into the pool. Its `descendants_*` fields are zero.
2. Insert `tx_B` (child of `tx_A`, high fee, size=200). `tx_A.descendants_fee` is correctly incremented via `update_ancestors_index_key` during insertion.
3. Submit `tx_B'` (spends the same input as `tx_B`, fee > `tx_B` satisfying RBF rules). `resolve_conflict` calls `remove_entry_and_descendants(tx_B)`.
4. After step 3: `tx_B` is gone, but `tx_A.descendants_fee` / `descendants_size` / `descendants_cycles` / `descendants_count` remain at their pre-removal values. `tx_A.evict_key` is stale.
5. Fill the pool to trigger `limit_size`. `next_evict_entry` iterates by `evict_key`; `tx_A`'s inflated `descendants_feerate` causes it to be ranked above legitimate transactions with lower (but accurate) fee-rates. Those legitimate transactions are evicted instead of `tx_A`.

A unit test can assert that after step 3, `pool_map.get_by_id(tx_A_id).inner.descendants_count == 0` and `descendants_fee == 0`; the bug causes both assertions to fail.