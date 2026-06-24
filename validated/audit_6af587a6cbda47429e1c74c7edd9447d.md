Audit Report

## Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Keys — (File: tx-pool/src/component/pool_map.rs)

## Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries from `self.links` before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`, and those links are already gone, the call returns an empty set. Ancestor entries that remain in the pool are never updated: their `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and derived `evict_key` permanently reflect the removed descendants. This corrupts eviction ordering in `limit_size`, allowing low-fee ancestors to appear high-value and causing legitimate high-fee transactions to be rejected with `Reject::Full`.

## Finding Description

**Phase 1** of `remove_entry_and_descendants` (L252–265) iterates every entry in the subtree and calls `remove_entry_links(id)` for each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // removes id from self.links entirely
}
```

`remove_entry_links` (L418–430) removes the entry from its parents' children sets, from its children's parents sets, and then calls `self.links.remove(id)`, fully erasing the entry from `TxLinksMap`.

**Phase 2** then calls `remove_entry(id)` for each removed entry. Inside `remove_entry` (L235–250), the first call is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` (L432–445) resolves ancestors via:

```rust
let ancestors: HashSet<ProposalShortId> =
    self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` calls `calc_relative_ids` (links.rs L37–50), which looks up the entry in `self.links.inner`. Since Phase 1 already called `self.links.remove(id)` for every entry in the subtree, the lookup returns `None`, the initial `direct` set is empty, and `calc_relation_ids` returns an empty `HashSet`. The loop body that calls `e.inner.sub_descendant_weight(child)` and updates `e.evict_key` is never reached for any ancestor that remains in the pool.

The comment in the source (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) confirms the pre-removal is intentional to skip updating descendants (which are being removed anyway), but it inadvertently also disables the ancestor update path.

The existing test `test_remove_entry_and_descendants` (score_key.rs L171–230) only asserts that tx2 and tx3 are absent and that tx1's descendants link set is empty. It never checks `tx1.descendants_count`, `tx1.descendants_fee`, or `tx1.evict_key`, so the stale accounting is not caught.

## Impact Explanation

`limit_size` (pool.rs L292–329) evicts entries by calling `next_evict_entry`, which iterates entries ordered by `evict_key`. An ancestor whose high-fee descendants were removed still carries their fee contribution in `descendants_fee`. Its `evict_key.fee_rate = descendants_feerate.max(feerate)` (entry.rs L243) is inflated, so it sorts as more valuable than it actually is and is skipped during eviction. The pool fills with these stale-accounting low-fee ancestors, causing legitimate high-fee transactions to be rejected with `Reject::Full`.

This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

`remove_entry_and_descendants` is called from multiple reachable paths. The most accessible to an unprivileged attacker is `check_and_record_ancestors` (pool_map.rs L588–639): when a submitted transaction exceeds `max_ancestors_count` and has `cell_ref_parents`, the code evicts those parents via `remove_entry_and_descendants`. An attacker can craft a transaction chain (tx_A low-fee parent → tx_B high-fee child that is also a cell dep) and then submit a new transaction that triggers eviction of tx_B, leaving tx_A with stale inflated accounting. This requires no mining power and can be repeated cheaply to saturate the pool with stale-accounting entries.

## Recommendation

Before pre-removing links in `remove_entry_and_descendants`, collect the set of external ancestors — ancestors of the root entry that are not themselves in `removed_ids` — and call `sub_descendant_weight` + update `evict_key` on each of them for every entry being removed. Alternatively, restructure the function to call `update_ancestors_index_key` for the root entry before any `remove_entry_links` calls (since descendants' ancestor fields need not be updated — they are being removed). The comment's stated intent (skip `update_descendants_index_key` for entries being removed) can still be achieved by a targeted guard rather than blanket pre-removal of all links.

## Proof of Concept

```
Setup:
  tx_A: fee=1 shannon, size=100  (no parents)
  tx_B: fee=1000 shannons, size=100  (child of tx_A)

After add_entry(tx_A) then add_entry(tx_B):
  tx_A.descendants_fee   = 1001 shannons
  tx_A.descendants_count = 2
  tx_A.evict_key.fee_rate ≈ 1001/200  (high)

Trigger (unprivileged path):
  Submit tx_C that has tx_B as a cell_dep and exceeds max_ancestors_count.
  → check_and_record_ancestors evicts tx_B via remove_entry_and_descendants(tx_B.id)

After removal:
  tx_B is gone.
  tx_A.descendants_fee   = 1001 shannons  ← STALE (should be 1)
  tx_A.descendants_count = 2              ← STALE (should be 1)
  tx_A.evict_key.fee_rate ≈ 1001/200      ← STALE (should be 1/100)

Repeat to fill pool with stale-accounting tx_A entries.
limit_size() calls next_evict_entry() — tx_A entries are skipped.
A legitimate tx with fee=500 shannons is rejected with Reject::Full.
```

A unit test can confirm this by asserting `tx1.descendants_count == 1` and `tx1.descendants_fee == 1 shannon` after calling `map.remove_entry_and_descendants(&tx2_id)` in the existing `test_remove_entry_and_descendants` fixture — this assertion will fail against the current code.