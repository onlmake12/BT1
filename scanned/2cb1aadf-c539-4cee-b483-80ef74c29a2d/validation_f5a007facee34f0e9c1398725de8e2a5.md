Audit Report

## Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Keys — (File: tx-pool/src/component/pool_map.rs)

## Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries from `self.links` for every entry in the subtree before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`, and those links are already gone, the function returns an empty set for every removed entry. Ancestor entries that remain in the pool are never updated: their `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and `evict_key` stay permanently inflated. This corrupts eviction ordering in `limit_size`, allowing an attacker to fill the pool with zombie low-fee ancestors that appear high-value, causing legitimate transactions to be rejected with `Reject::Full`.

## Finding Description

**Root cause — Phase 1 destroys the link graph before Phase 2 can use it:**

`remove_entry_and_descendants` (pool_map.rs L252–265) first iterates all entries in `removed_ids` and calls `remove_entry_links` on each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // removes id from self.links entirely
}
```

`remove_entry_links` (L418–430) calls `self.links.remove(id)` (L429), which deletes the entry's record from `TxLinksMap::inner` entirely.

Phase 2 then calls `remove_entry` for each id (L261–264). Inside `remove_entry` (L242), the first call is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` (L432–445) resolves ancestors via:

```rust
let ancestors: HashSet<ProposalShortId> =
    self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` (links.rs L78–80) calls `calc_relative_ids`, which does:

```rust
let direct = self.inner.get(short_id)   // returns None — already removed
    .map(|link| link.get_direct_ids(relation))
    .cloned()
    .unwrap_or_default();               // empty set
```

Because the entry was removed from `self.links` in Phase 1, `calc_ancestors` returns an empty `HashSet`. The loop body in `update_ancestors_index_key` (L435–444) that calls `e.inner.sub_descendant_weight(child)` and updates `e.evict_key` is never reached for any external ancestor.

**Consequence — stale fields on surviving ancestors:**

`sub_descendant_weight` (entry.rs L133–142) decrements `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`. None of these decrements happen. `EvictKey` (entry.rs L234–247) is computed from those stale fields:

```rust
EvictKey {
    fee_rate: descendants_feerate.max(feerate),  // uses stale descendants_fee/size/cycles
    descendants_count: entry.descendants_count,  // stale
    ...
}
```

**Why existing checks are insufficient:**

The comment in the code (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) documents the *intent* of Phase 1 — to suppress `update_descendants_index_key` for entries being removed. However, it inadvertently also suppresses `update_ancestors_index_key` for entries that **remain** in the pool. There is no compensating update for external ancestors anywhere in the function.

## Impact Explanation

`limit_size` (pool.rs L292–328) evicts entries by calling `next_evict_entry`, which iterates `entries` ordered by `evict_key` ascending (lowest fee-rate first). An ancestor whose high-fee descendants were removed still carries their fee contribution in `descendants_fee`, so its `evict_key.fee_rate` is inflated. It sorts as more valuable than it actually is and is skipped during eviction. The pool accumulates zombie low-fee ancestors that cannot be evicted, causing legitimate high-fee transactions to be rejected with `Reject::Full`.

This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The bug is reachable by an unprivileged user via at least two paths:

1. **`limit_size` → `remove_entry_and_descendants`** (pool.rs L307): Any tx submitter who fills the pool triggers eviction. If the evicted entry has a surviving parent, that parent's `evict_key` becomes stale. No mining power required.
2. **`resolve_conflict` → `remove_entry_and_descendants`** (pool_map.rs L285): Any committed block that spends an input already in the pool triggers this. An attacker who can submit a valid block (or observe natural block production) can exploit this.

The attack is repeatable: each cycle of submit-parent + submit-child + trigger-removal-of-child leaves one more zombie ancestor in the pool. Cost is proportional to the number of transactions submitted, which is low relative to the pool capacity.

## Recommendation

Before Phase 1 pre-removes links, collect the set of **external ancestors** — ancestors of the root entry that are not themselves in `removed_ids` — and for each entry being removed, call `sub_descendant_weight` and update `evict_key` on those external ancestors directly. Alternatively, restructure the function to call `update_ancestors_index_key` on the root entry only (before its links are removed), since descendants' ancestor fields need not be updated as they are being removed. The key invariant to restore: every entry that remains in the pool after `remove_entry_and_descendants` must have accurate `descendants_*` fields and a correct `evict_key`.

## Proof of Concept

```
Setup:
  tx_A (fee=1 shannon, size=100) added to pool
  tx_B (fee=1000 shannons, size=100) added as child of tx_A

After add_entry(tx_A) then add_entry(tx_B):
  tx_A.descendants_fee   = 1001 shannons
  tx_A.descendants_count = 2
  tx_A.evict_key.fee_rate ≈ 1001/200 (high)

Trigger: remove_entry_and_descendants(tx_B)
  (reachable via limit_size or resolve_conflict)

After removal:
  tx_B is gone.
  tx_A.descendants_fee   = 1001 shannons  ← STALE (should be 1)
  tx_A.descendants_count = 2              ← STALE (should be 1)
  tx_A.evict_key.fee_rate ≈ 1001/200      ← STALE (should be 1/100)

Pool fills. limit_size() calls next_evict_entry().
tx_A is skipped because its evict_key shows high fee rate.
A legitimate tx with fee=500 shannons is rejected with Reject::Full.
```

A unit test can be written in `tx-pool/src/component/tests/score_key.rs` following the pattern of `test_remove_entry`: add tx_A, add tx_B as child, call `remove_entry_and_descendants(&tx_B_id)`, then assert `pool.get(&tx_A_id).unwrap().descendants_count == 1` and `pool.get(&tx_A_id).unwrap().descendants_fee == tx_A_fee`. This assertion will fail on the current code, confirming the bug.