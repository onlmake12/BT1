The code is confirmed. Let me verify the eviction usage and any existing guards.

Audit Report

## Title
Ancestor Descendant-Weight Not Decremented After `remove_entry_and_descendants` Pre-Clears Links — (`tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` pre-clears all link entries via `remove_entry_links` before calling `remove_entry` on each removed tx. When `remove_entry` subsequently calls `update_ancestors_index_key`, `calc_ancestors` looks up the already-removed ID in `links.inner`, finds nothing, and returns an empty set. As a result, `sub_descendant_weight` is never called on surviving ancestors, leaving their `descendants_*` fields and `evict_key` permanently inflated until those ancestors are themselves removed.

## Finding Description

In `remove_entry_and_descendants` (lines 252–265), all IDs in the removal set have `remove_entry_links` called on them first, then `remove_entry` is called for each. `remove_entry_links` (lines 418–430) calls `self.links.remove(id)`, which removes the entry from `links.inner`. When `remove_entry` (lines 235–250) subsequently calls `update_ancestors_index_key`, that function calls `self.links.calc_ancestors(&child.proposal_short_id())` (line 434). `calc_ancestors` delegates to `calc_relative_ids` (links.rs lines 37–50), which does `self.inner.get(short_id)` — returning `None` because the entry was already removed — and returns an empty `HashSet`. The `for anc_id in &ancestors` loop (line 435) never executes, so `sub_descendant_weight` (entry.rs lines 133–142) and the `evict_key` refresh (`e.evict_key = e.inner.as_evict_key()`, line 442) are skipped for all surviving ancestors.

Concrete scenario: `tx_parent` is in the pool; `tx_child` spends `tx_parent`'s output. A confirmed block conflicts with `tx_child` (but not `tx_parent`). `resolve_conflict` (lines 305–332) calls `remove_entry_and_descendants(&tx_child_id)`. After the call, `tx_parent.descendants_count` remains 2 instead of 1, and `descendants_fee/size/cycles` remain inflated. The comment on line 256 acknowledges the pre-clear is intentional to suppress `update_descendants_index_key` for co-removed txs, but it inadvertently also suppresses `update_ancestors_index_key` for ancestors that are **not** being removed.

## Impact Explanation

`EvictKey` is derived from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` (entry.rs lines 234–247). With inflated `descendants_*`, `tx_parent`'s `EvictKey.fee_rate` (`descendants_feerate.max(feerate)`) is higher than its true value. `next_evict_entry` (lines 380–385) iterates `iter_by_evict_key()` ascending, so `tx_parent` is pushed toward the back of the eviction queue. When the pool is full, transactions with genuinely higher fee rates may be incorrectly evicted in its place. This is a concrete correctness defect in the tx pool eviction mechanism, fitting **Low (501–2000 points): any other important performance improvements for CKB**.

## Likelihood Explanation

This triggers on every confirmed block that conflicts with a child transaction whose parent remains in the pool — a routine occurrence during normal chain operation. No special attacker capability is needed: submit a parent-child tx chain via P2P relay, then wait for any legitimately mined block that spends the same input as the child. The bug is deterministic and locally reproducible. The stale state persists until `tx_parent` is itself removed.

## Recommendation

In `remove_entry_and_descendants`, update surviving ancestors' descendant weights **before** clearing links. One concrete approach: for each ID in `removed_ids`, call `update_ancestors_index_key` (while links are still intact) filtering out IDs that are also in `removed_ids`, then proceed with `remove_entry_links` and the rest of `remove_entry` cleanup. Alternatively, pass the full set of removed IDs into `update_ancestors_index_key` so it can skip updating ancestors that are themselves being removed.

## Proof of Concept

```
1. Add tx_parent to pool (descendants_count = 1, descendants_fee = F_p)
2. Add tx_child spending tx_parent's output
   → tx_parent: descendants_count = 2, descendants_fee = F_p + F_c
3. Call pool_map.remove_entry_and_descendants(&tx_child_id)
   a. remove_entry_links(tx_child_id):
      - links.remove_child(&tx_parent_id, &tx_child_id)
      - links.remove(&tx_child_id)  ← tx_child gone from links.inner
   b. remove_entry(tx_child_id):
      - update_ancestors_index_key(&tx_child_entry, Remove)
        → calc_ancestors(&tx_child_id)
        → calc_relative_ids: self.inner.get(&tx_child_id) == None
        → returns {}
        → loop body never executes
        → sub_descendant_weight never called on tx_parent
4. Assert tx_parent.descendants_count == 1  → FAILS, still 2
5. Assert tx_parent.descendants_fee == F_p  → FAILS, still F_p + F_c
6. Assert tx_parent.evict_key reflects only tx_parent's own fee rate → FAILS, still inflated
```