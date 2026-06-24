The code is confirmed. Let me trace the exact execution path carefully before rendering a verdict.

**Trace of `remove_entry_and_descendants`** (lines 252–265):

1. `removed_ids` = `[root_id]` + all descendants via `calc_descendants`
2. First loop: `remove_entry_links(id)` is called for **every** id in `removed_ids`
   - `remove_entry_links` (lines 418–430): removes `id` from parents' children sets, removes `id` from children's parents sets, then calls `self.links.remove(id)` — deleting the entry's own record from `self.links.inner`
3. Second loop: `remove_entry(id)` is called for each id
   - Inside `remove_entry` (line 242): `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` is called
   - Inside `update_ancestors_index_key` (line 433–434): `self.links.calc_ancestors(&child.proposal_short_id())` is called
   - `calc_ancestors` → `calc_relative_ids` (links.rs lines 42–47): `self.inner.get(short_id)` returns `None` because `remove_entry_links` already deleted the record → `.unwrap_or_default()` returns empty `HashSet`
   - Result: **zero ancestors found**, `evict_key` update loop never executes

The surviving ancestors of the root (parents NOT in `removed_ids`) never receive `sub_descendant_weight` + `evict_key` recomputation. Their `evict_key.fee_rate` remains `max(descendants_feerate_old, own_feerate)` and `descendants_count` remains inflated.

`next_evict_entry` (lines 380–385) iterates `iter_by_evict_key()` ascending — lowest `fee_rate` first. A stale-inflated `evict_key` pushes the surviving ancestor toward the **high** end of the index, making it appear more valuable than it is and shielding it from eviction.

**Impact assessment against allowed scope:**

The corruption is real and persistent (it is never self-correcting unless the ancestor is itself later removed). However, the concrete consequence is incorrect eviction ordering in the mempool — low-fee ancestors are shielded from eviction, potentially displacing higher-fee transactions when the pool is full. This does not crash a node, does not cause consensus deviation, and does not directly damage the CKB economy in a provable on-chain sense. The closest allowed impact is "Low (501–2000): Any other important performance improvements for CKB," or at a stretch "Medium: Suboptimal implementation of CKB state storage mechanism," but the tx-pool is not the state storage layer. The claim's own impact section is truncated and does not complete a concrete proof of a Critical/High impact.

---

Audit Report

## Title
Stale `evict_key` on Surviving Ancestors After Batch Descendant Removal — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` tears down all link records via `remove_entry_links` for every entry in the batch before calling `remove_entry` on any of them. Because `update_ancestors_index_key` relies on `self.links.calc_ancestors` to locate surviving ancestors, and those link records are already gone, it silently returns an empty set and skips the `evict_key` update entirely. Every surviving ancestor of the removed subtree retains a stale, inflated `evict_key` that still reflects the removed descendants' fee-rate and count, corrupting the eviction-priority ordering of the pool.

## Finding Description
`remove_entry_and_descendants` (pool_map.rs L252–265) first iterates all `removed_ids` and calls `remove_entry_links` on each (L257–259). `remove_entry_links` (L418–430) removes the entry from its parents' and children's sets and then calls `self.links.remove(id)`, deleting the entry's own record from `self.links.inner`. In the second pass, `remove_entry` (L235–250) is called for each id. At L242, `update_ancestors_index_key` is invoked, which calls `self.links.calc_ancestors(&child.proposal_short_id())` (L433–434). `calc_ancestors` delegates to `calc_relative_ids` (links.rs L37–50), which does `self.inner.get(short_id)` — returning `None` because the record was already deleted — and falls through to `unwrap_or_default()`, yielding an empty `HashSet`. The ancestor loop in `update_ancestors_index_key` (L435–444) therefore never executes. Surviving ancestors (parents of the root that are not in `removed_ids`) never receive `sub_descendant_weight` nor have their `evict_key` recomputed via `e.inner.as_evict_key()` (L442). The comment at L256 acknowledges only the intent to suppress redundant `update_descendants_index_key` calls for entries being removed, but it also silently suppresses the necessary `update_ancestors_index_key` for entries that are **not** being removed.

## Impact Explanation
After `remove_entry_and_descendants` returns, every surviving ancestor `P` has `evict_key.fee_rate = max(descendants_feerate_old, own_feerate)` and an inflated `descendants_count`. `next_evict_entry` (L380–385) iterates `iter_by_evict_key()` in ascending order (lowest fee_rate first per sort_key.rs L92–104). The stale-inflated key pushes `P` toward the high end of the eviction index, shielding it from eviction. When the pool is full, `P` is skipped in favour of entries that should have been retained. This is a correctness defect in pool eviction ordering, matching **Low (501–2000 points): Any other important performance improvements for CKB**.

## Likelihood Explanation
The bug is triggered whenever `remove_entry_and_descendants` is called on a transaction that has at least one surviving ancestor in the pool. This occurs via `resolve_conflict` (L305–332) and `resolve_conflict_header_dep` (L267–292), both reachable by any unprivileged user submitting a conflicting transaction. No special privileges are required. The stale state persists until the ancestor is itself removed.

## Recommendation
Before the batch `remove_entry_links` loop, collect and snapshot the surviving ancestors of the root (i.e., `calc_ancestors(root_id)` minus `removed_ids`). After all entries are removed, iterate those surviving ancestors and recompute their `evict_key` directly via `modify_by_id` with `sub_descendant_weight` for each removed entry. Alternatively, restructure `remove_entry_and_descendants` to call `update_ancestors_index_key` for the root entry **before** tearing down any link records, then suppress only the descendant-index updates (which is the stated intent of the existing comment).

## Proof of Concept
1. Insert ancestor `A` (low fee-rate) into the pool.
2. Insert child `C` of `A` (high fee-rate); `A.evict_key.fee_rate` is now elevated to `C`'s fee-rate.
3. Submit a conflicting transaction that triggers `resolve_conflict` → `remove_entry_and_descendants(&C_id)`.
4. Assert `pool_map.get_by_id(&A_id).unwrap().evict_key == A.inner.as_evict_key()` — this assertion **fails** because `A.evict_key` still reflects `C`'s fee-rate contribution.
5. Insert a new entry `X` with fee-rate between `A`'s true rate and the stale rate; call `next_evict_entry` — `X` is returned for eviction instead of `A`, demonstrating the corrupted ordering.