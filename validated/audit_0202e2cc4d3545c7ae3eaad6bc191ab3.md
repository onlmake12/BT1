Audit Report

## Title
Stale `evict_key` on Surviving Ancestors After Batch Descendant Removal — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-emptively tears down all link-graph records for every entry in the batch before calling `remove_entry` on any of them. Because `update_ancestors_index_key` relies on `self.links.calc_ancestors` to locate surviving ancestors, and those link records are already gone, the function silently finds no ancestors and skips the `evict_key` update entirely. Every surviving ancestor of the removed subtree is left with a permanently inflated `evict_key`, corrupting the pool's eviction-priority ordering.

## Finding Description

**Root cause — link teardown precedes ancestor update.**

`remove_entry_and_descendants` (L252–265) first iterates all `removed_ids` and calls `remove_entry_links` on each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // deletes self.links.inner[id]
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) calls `self.links.remove(id)` as its last step, which removes the entry from `TxLinksMap::inner` entirely.

`remove_entry` (L235–250) then calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` (L242), which calls:

```rust
let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` → `calc_relative_ids` (links.rs L37–50) does:

```rust
let direct = self.inner.get(short_id)   // returns None — record already deleted
    .map(|link| link.get_direct_ids(relation))
    .cloned()
    .unwrap_or_default();               // empty set
self.calc_relation_ids(direct, relation) // returns empty set
```

Because `self.inner.get(&root_id)` returns `None` (the record was deleted in the pre-pass), `calc_ancestors` returns an empty `HashSet`. The `for anc_id in &ancestors` loop in `update_ancestors_index_key` (L435) never executes. Surviving ancestors — those **not** in `removed_ids` — never have `sub_descendant_weight` called on them and their `evict_key` is never refreshed.

**Contrast with the single-entry path.**

`remove_entry` called standalone (L235–250) executes in the correct order:
1. Remove from `entries`
2. `update_ancestors_index_key` — link graph still intact, ancestors found, `evict_key` updated
3. `update_descendants_index_key`
4. `remove_entry_edges`
5. `remove_entry_links` — link graph torn down last

The batch path inverts step 5 to before step 2 for every entry simultaneously.

**Stale state after the call.**

For any surviving ancestor `P` of the removed subtree, after `remove_entry_and_descendants` returns:
- `P.inner.descendants_count` is still inflated (never had `sub_descendant_weight` called)
- `P.evict_key.fee_rate` = `max(old_descendants_feerate, own_feerate)` instead of `own_feerate`
- `P.evict_key.descendants_count` is still inflated

`EvictKey::cmp` (sort_key.rs L92–104) orders ascending by `fee_rate` then `descendants_count`. An inflated `evict_key` places `P` toward the **high** end of the eviction index, making it appear more valuable than it actually is.

**Callers that trigger the bug in normal operation:**
- `resolve_conflict` (L305–332): called whenever a new transaction double-spends an existing pool entry
- `resolve_conflict_header_dep` (L267–292): called on fork/reorg events
- `check_and_record_ancestors` (L618): called when ancestor-count limits are exceeded during insertion

## Impact Explanation

After the bug fires, `next_evict_entry` (L380–385) iterates `iter_by_evict_key()` in ascending order. `P`, with its stale inflated `evict_key`, is sorted toward the high end and skipped during eviction. Transactions with genuinely higher fee rates may be evicted in its place. The pool's eviction ordering is persistently incorrect until `P` itself is eventually removed. This constitutes a **suboptimal implementation of the CKB tx-pool state management mechanism**, matching the **Medium (2001–10000 points)** impact tier.

## Likelihood Explanation

The bug fires in ordinary node operation: every time a conflicting transaction is submitted (double-spend replacement), `resolve_conflict` calls `remove_entry_and_descendants`, and any surviving parent of the removed chain is affected. No special attacker capability is required beyond submitting standard transactions. The condition is repeatable and deterministic.

## Recommendation

Before the pre-pass loop that calls `remove_entry_links`, collect and update the surviving ancestors' `evict_key` first. Concretely, for the root entry `id`, compute `self.links.calc_ancestors(id)` while the link graph is still intact, then for each removed entry call `sub_descendant_weight` on those ancestors and refresh their `evict_key`. Only after that should `remove_entry_links` be called for the batch. Alternatively, restructure `remove_entry_and_descendants` to call `update_ancestors_index_key` for the root entry before any link teardown, mirroring the correct ordering already present in the single-entry `remove_entry` path.

## Proof of Concept

Minimal unit test plan:

1. Insert ancestor `P` (low fee rate, e.g. 1 shannon/byte).
2. Insert child `A` of `P` (high fee rate, e.g. 1000 shannons/byte).
3. Insert grandchild `B` of `A` (high fee rate).
4. Assert `P.evict_key.fee_rate` reflects descendants (inflated).
5. Call `pool_map.remove_entry_and_descendants(&A_id)`.
6. Retrieve `P`'s `PoolEntry` from `entries`.
7. **Assert (fails before fix):** `P.evict_key.fee_rate == P.inner.fee_rate()` and `P.evict_key.descendants_count == 1`.
8. **Assert (fails before fix):** `P.inner.descendants_count == 1`, `P.inner.descendants_fee == P.inner.fee`.

Steps 7–8 will fail on the current code because `sub_descendant_weight` was never called on `P`, confirming the stale state.