The code is confirmed. Let me trace through the exact execution path to validate the claim.

Audit Report

## Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor Eviction Keys Inflated - (File: tx-pool/src/component/pool_map.rs)

## Summary
`PoolMap::remove_entry_and_descendants` strips all link entries for the entire removed cluster before invoking `remove_entry` on each member. Because `update_ancestors_index_key` resolves ancestors through the live link graph, and those links are already gone, it returns an empty ancestor set and never calls `sub_descendant_weight` on surviving ancestors. Those ancestors retain permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields, which feed directly into their `EvictKey`, causing the pool's eviction logic to rank them as more valuable than they actually are.

## Finding Description
`remove_entry_and_descendants` (pool_map.rs L252–265) first iterates over the root plus all descendants and calls `remove_entry_links` on every one of them. `remove_entry_links` (L418–430) calls `self.links.remove(id)` as its final step, which deletes the entry from `TxLinksMap::inner`. Only after all links are stripped does the function call `remove_entry` for each member.

Inside `remove_entry` (L235–250), `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` is called at L242. That function (L432–445) calls `self.links.calc_ancestors(&child.proposal_short_id())`. `calc_ancestors` delegates to `calc_relative_ids` (links.rs L37–50), which begins with `self.inner.get(short_id)`. Because `remove_entry_links` already removed the entry from `self.links.inner`, this lookup returns `None`, `direct` is an empty set, and `calc_relation_ids` returns an empty set. The `for anc_id in &ancestors` loop body never executes; no ancestor receives `sub_descendant_weight`.

For the single-entry path (`remove_entry` called directly), the order is correct: `update_ancestors_index_key` is called at L242 before `remove_entry_links` at L245, so the link graph is still intact when ancestors are resolved. `remove_entry_and_descendants` breaks this invariant by pre-stripping links.

The stale `descendants_*` values propagate into `EvictKey` via `as_evict_key()` (entry.rs L234–247), which computes `fee_rate` as `descendants_feerate.max(feerate)`. `EvictKey::cmp` (sort_key.rs L92–103) orders entries ascending by `fee_rate`, so an ancestor with an inflated `descendants_feerate` sorts higher and is skipped by `next_evict_entry` (pool_map.rs L380–385), which picks the lowest-keyed entry for eviction.

## Impact Explanation
Surviving ancestors of any removed cluster carry stale `descendants_*` fields indefinitely. Their `EvictKey` is inflated, so `limit_size` (pool.rs L292–329) will not select them for eviction when the pool is full. Legitimate higher-fee transactions submitted afterward are rejected with `Reject::Full`. This is a concrete, persistent mempool correctness defect reachable by any unprivileged submitter, qualifying as an important correctness/performance improvement for CKB (Low, 501–2000 points).

## Likelihood Explanation
`remove_entry_and_descendants` is called from `limit_size`, `resolve_conflict`, `resolve_conflict_header_dep`, `check_and_record_ancestors`, `remove_by_detached_proposal`, and the `remove_transaction` RPC. The simplest trigger requires only two submitted transactions (a parent and a child) followed by an RBF replacement of the child, all via the standard relay protocol. No privileged access, key material, or majority hash power is required. The stale state persists until the ancestor is itself removed or the pool is cleared, so the window of incorrect eviction decisions can be arbitrarily long.

## Recommendation
In `remove_entry_and_descendants`, resolve and update the surviving ancestors of the root entry **before** any links are stripped. Concretely, call `update_ancestors_index_key(&root_entry, EntryOp::Remove)` (or equivalently, collect the ancestor set via `calc_ancestors(root_id)` and apply `sub_descendant_weight` to each) while the link graph is still intact, then proceed with the bulk `remove_entry_links` loop and the per-entry `remove_entry` calls. The descendants-only entries do not need ancestor updates because they are all being removed, so the existing optimization of suppressing `update_descendants_index_key` remains valid.

## Proof of Concept
```
1. Insert tx_A (fee=100, size=200) into the pool with no parents.
   → tx_A: descendants_count=1, descendants_fee=100, evict_key reflects fee_rate=100/weight(200,cycles).

2. Insert tx_B (fee=1000, size=200) spending tx_A's output.
   → record_entry_descendants calls update_ancestors_index_key(tx_B, Add).
   → tx_A: descendants_count=2, descendants_fee=1100, evict_key inflated to reflect descendants_feerate.

3. Submit tx_C conflicting with tx_B (higher fee, valid RBF).
   → resolve_conflict calls remove_entry_and_descendants(tx_B.id).
   → Loop: remove_entry_links(tx_B.id) → self.links.remove(tx_B.id); tx_B gone from link graph.
   → remove_entry(tx_B.id) → update_ancestors_index_key(tx_B, Remove)
       → calc_ancestors(tx_B.id) → self.links.inner.get(tx_B.id) == None → returns {}.
       → No sub_descendant_weight called on tx_A.
   → tx_A: descendants_count=2, descendants_fee=1100 (STALE).

4. Pool fills to capacity. limit_size calls next_evict_entry.
   → tx_A.evict_key.fee_rate = descendants_feerate(1100, weight(400,…)) — inflated.
   → tx_A is not the minimum evict_key entry; it is skipped.
   → A legitimate high-fee tx_D is rejected with Reject::Full instead.

Invariant assertion: after step 3, tx_A.descendants_count should equal 1 and
tx_A.descendants_fee should equal 100; both remain at 2 and 1100 respectively.
```