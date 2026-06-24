The code confirms the claim. All referenced mechanics are verified:

1. `remove_entry_and_descendants` (lines 252–265) tears down all links first, then calls `remove_entry` on each member.
2. `remove_entry` (line 242) calls `update_ancestors_index_key`, which calls `calc_ancestors` (line 434), which calls `calc_relative_ids` in `links.rs` (lines 42–47) — returning an empty set via `unwrap_or_default()` because the link record is already gone.
3. `EvictKey::from` (entry.rs lines 234–247) computes `fee_rate` as `max(descendants_feerate, feerate)` using stale `descendants_fee`/`descendants_size`/`descendants_cycles`, and stores stale `descendants_count` — both fields corrupted.
4. `EvictKey::cmp` (sort_key.rs lines 92–104) uses `fee_rate` then `descendants_count` — stale inflated values cause A to sort higher (harder to evict).
5. The existing `test_remove_entry_and_descendants` test (score_key.rs lines 171–230) only asserts B and C are absent and A's descendants set is empty — it never checks A's `descendants_count`, `descendants_fee`, or `evict_key`.

---

Audit Report

## Title
Stale Descendant Statistics in `PoolMap::remove_entry_and_descendants` Corrupt Eviction Ordering - (File: tx-pool/src/component/pool_map.rs)

## Summary

`remove_entry_and_descendants` tears down all link records for the entire removal batch before calling `remove_entry` on each member. Because `update_ancestors_index_key` relies on those link records to walk up to ancestors, it receives an empty ancestor set and never calls `sub_descendant_weight` or refreshes the `evict_key` of any ancestor outside the removed subtree. Those ancestors permanently carry inflated `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles`, and a stale `evict_key`, corrupting the eviction order used when the tx-pool is full.

## Finding Description

`remove_entry_and_descendants` (lines 252–265) first iterates over all removed IDs and calls `remove_entry_links` on each, then calls `remove_entry` on each:

```rust
// pool_map.rs lines 256-264
for id in &removed_ids {
    self.remove_entry_links(id);   // tears down link record for every member
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (lines 418–430) calls `self.links.remove(id)`, deleting the entry's record from `TxLinksMap::inner`. When `remove_entry` subsequently calls `update_ancestors_index_key` (line 242), that function calls `self.links.calc_ancestors(&child.proposal_short_id())` (line 434). `calc_ancestors` delegates to `calc_relative_ids` (links.rs lines 37–50), which does `self.inner.get(short_id).map(...).unwrap_or_default()` — returning an empty set because the record is already gone. No ancestor outside the removed batch is ever reached, so `sub_descendant_weight` and the `evict_key` refresh (lines 439–442) are never executed for them.

The developer comment at line 256 — *"update links state for remove, so that we won't update_descendants_index_key in remove_entry"* — confirms the intent was only to suppress the redundant `update_descendants_index_key` call. The side effect of also silencing `update_ancestors_index_key` for outside ancestors is the defect.

For a chain **A → B → C**, calling `remove_entry_and_descendants(B)`:
1. `remove_entry_links(B)` and `remove_entry_links(C)` are called — both link records deleted.
2. `remove_entry(B)` → `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B)` returns `{}` → A is never reached.
3. A's `descendants_count` stays at 3 (should be 1), `descendants_fee`/`descendants_size`/`descendants_cycles` remain inflated, and `evict_key` is never refreshed.

`EvictKey::from` (entry.rs lines 234–247) computes `fee_rate` as `max(descendants_feerate, feerate)` using the stale descendant fields, so A's stored `fee_rate` in `EvictKey` is also inflated. `EvictKey::cmp` (sort_key.rs lines 92–104) uses `fee_rate` first, then `descendants_count` as a tiebreaker. A stale high `fee_rate` and `descendants_count` on A causes it to sort as harder to evict than it actually is, so `next_evict_entry` → `iter_by_evict_key()` (lines 380–385) skips A in favour of a different victim.

No corrective code path recomputes or resets the stale `evict_key` after the fact.

## Impact Explanation

This is a suboptimal/incorrect implementation of CKB's tx-pool state management mechanism. The `evict_key` index is a stored, sorted field that directly drives victim selection when the pool reaches capacity. Stale values cause the wrong transaction to be evicted: a low-fee ancestor with artificially inflated `descendants_count` and `fee_rate` survives rounds it should lose, while a genuinely lower-priority transaction is evicted instead. The corruption is persistent — it lasts until the ancestor is itself removed. This matches **Medium (2001–10000 points): suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation

Any code path that calls `remove_entry_and_descendants` on an entry that has a pool-resident ancestor triggers the bug. This occurs routinely via `resolve_conflict` (lines 305–332), `resolve_conflict_header_dep` (lines 267–292), and `check_and_record_ancestors` (lines 588–640). An unprivileged submitter can deliberately construct a parent–child chain (A → B → C), then submit a conflicting transaction to evict B and C, leaving A with permanently stale stats. No special privilege is required.

## Recommendation

Snapshot each entry's ancestor set **before** any links are torn down, then apply the weight subtraction using the snapshot. The simplest approach: iterate the removed IDs once to call `update_ancestors_index_key` while links are still intact, then iterate again to call `remove_entry_links`, then call a variant of `remove_entry` that skips the now-redundant ancestor update:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update outside ancestors BEFORE links are torn down.
    for id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(id).map(|e| e.inner.clone()) {
            self.update_ancestors_index_key(&entry, EntryOp::Remove);
        }
    }

    // Now safe to remove links (descendants update in remove_entry will be a no-op).
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

## Proof of Concept

1. Build a `PoolMap` and add three transactions: tx1 (root, fee=1000), tx2 (spends tx1 output 0, fee=100), tx3 (spends tx2 output 0, fee=100).
2. Assert tx1's `descendants_count == 3`, `descendants_fee == 1200`.
3. Call `pool_map.remove_entry_and_descendants(&tx2.proposal_short_id())`.
4. Assert tx2 and tx3 are absent from the pool.
5. Retrieve tx1's entry and assert `descendants_count == 1` and `descendants_fee == 1000` — **this assertion will fail**, confirming the stale stats.
6. Assert tx1's `evict_key` equals `tx1.as_evict_key()` computed from the corrected stats — **this assertion will also fail**.

The existing `test_remove_entry_and_descendants` in `tx-pool/src/component/tests/score_key.rs` (lines 171–230) only checks that B and C are absent and that A's descendants set is empty; it never asserts A's `descendants_count`, `descendants_fee`, or `evict_key`, so the defect is not currently caught by the test suite.