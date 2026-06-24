Audit Report

## Title
Stale Ancestor `evict_key` After `remove_entry_and_descendants` Pre-Clears Links — (`tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` clears all transaction links in a pre-pass before calling `remove_entry` on each removed transaction. `remove_entry` calls `update_ancestors_index_key`, which uses `self.links.calc_ancestors(...)` to find remaining pool ancestors and decrement their descendant-weight fields and refresh their `evict_key`. Because the links are already gone at that point, `calc_ancestors` returns an empty set, the update loop never executes, and any ancestor that remains in the pool retains permanently inflated `descendants_count` and a stale `evict_key`.

## Finding Description

`remove_entry_and_descendants` (L252–265) first iterates over all removed IDs and calls `remove_entry_links` on each, which removes the entry from `self.links.inner` entirely and severs all parent/child cross-references. Only after this pre-pass does it call `remove_entry` for each ID.

Inside `remove_entry` (L235–250), `update_ancestors_index_key` is called with the removed entry. That function (L432–445) calls `self.links.calc_ancestors(&child.proposal_short_id())`, which delegates to `calc_relative_ids` (links.rs L37–50). `calc_relative_ids` does `self.inner.get(short_id)` — but the entry for `short_id` was already deleted by `remove_entry_links` in the pre-pass, so `get` returns `None`, `unwrap_or_default()` yields an empty set, and the ancestor-update loop body never runs.

The comment on the pre-pass (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) confirms the intent was only to suppress the descendant update (to avoid double-decrementing descendants that are themselves being removed). However, it inadvertently also suppresses the ancestor update, which is the bug.

`remove_entry_links` (L418–430) removes the entry from `self.links.inner` via `self.links.remove(id)` (links.rs L94–96), so any subsequent `calc_ancestors` call for that ID returns `∅`.

The two reachable call sites are `resolve_conflict` (L305–332) and `resolve_conflict_header_dep` (L267–292), both of which call `remove_entry_and_descendants` and are reachable by any unprivileged tx submitter.

## Impact Explanation

The `EvictKey` struct (sort_key.rs L80–84) contains `fee_rate`, `descendants_count`, and `timestamp`. Its `Ord` implementation (sort_key.rs L92–103) sorts first by `fee_rate`, then by `descendants_count` (ascending), then by `timestamp`. `next_evict_entry` (L380–385) iterates `iter_by_evict_key()` in ascending order, evicting the entry with the smallest key first.

An ancestor tx1 whose `descendants_count` is inflated (e.g., still 2 after its two descendants were removed) has a larger `evict_key` than it should. It is therefore ranked later in the eviction queue and is systematically protected from eviction relative to other transactions with the same `fee_rate`. Transactions with genuinely lower `evict_key` values may be evicted ahead of it. This corrupts the pool's eviction ordering persistently for the lifetime of tx1.

This matches the allowed impact: **Low (501–2000 points) — any other important performance/correctness improvement for CKB**, as the bug degrades the correctness of the tx-pool eviction mechanism without causing node crashes or consensus deviation.

## Likelihood Explanation

The trigger is trivial and requires no privilege: submit tx1 → tx2 to any node, then submit tx4 that spends the same input as tx2. `resolve_conflict` calls `remove_entry_and_descendants(tx2)`, leaving tx1 in the pool with a stale `evict_key`. The scenario is repeatable and constructable by any unprivileged user with minimal fee cost.

## Recommendation

Before the pre-clear loop in `remove_entry_and_descendants`, call `update_ancestors_index_key` for the root transaction (the one passed as `id`) while links are still intact. This ensures that all ancestors of the removed subtree have their descendant-weight fields and `evict_key` decremented correctly. Alternatively, collect the ancestor set for the root before clearing any links and pass it explicitly to a modified `remove_entry` variant that skips the `calc_ancestors` call and uses the pre-collected set instead.

## Proof of Concept

1. Insert tx1 (pending). Insert tx2 whose input spends tx1's output (pending). tx1's `descendants_count` = 1.
2. Submit tx4 spending the same input as tx2. `resolve_conflict` calls `remove_entry_and_descendants(&tx2_id)`.
3. Pre-pass: `remove_entry_links(tx2)` removes tx2 from tx1's children and removes tx2's link entry. `remove_entry_links` for any further descendants similarly clears their entries.
4. `remove_entry(tx2)` → `update_ancestors_index_key(tx2, Remove)` → `calc_ancestors(tx2)` → `self.links.inner.get(tx2)` returns `None` → empty set → loop skipped → tx1's `descendants_count` remains 1, `evict_key` is not refreshed.
5. Assert: `pool_map.get(&tx1_id).unwrap().descendants_count == 1` (should be 0). `next_evict_entry` returns a different transaction ahead of tx1 when the pool fills, demonstrating incorrect eviction ordering.

A unit test in `tx-pool/src/component/pool_map.rs` can verify this by inserting a two-tx chain, calling `remove_entry_and_descendants` on the child, and asserting that the parent's `descendants_count` is 0 and its `evict_key` reflects zero descendants.