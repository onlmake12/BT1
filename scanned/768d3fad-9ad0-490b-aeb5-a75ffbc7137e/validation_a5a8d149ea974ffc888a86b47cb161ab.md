The code confirms the claim. Let me verify the key mechanics:

Audit Report

## Title
Stale Descendant Statistics in `PoolMap::remove_entry_and_descendants` Corrupt Eviction Ordering - (File: tx-pool/src/component/pool_map.rs)

## Summary

`remove_entry_and_descendants` tears down all link records for the entire removal batch before calling `remove_entry` on each member. Because `update_ancestors_index_key` relies on those link records to walk up to ancestors, it receives an empty ancestor set and never updates `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles`, or the stored `evict_key` of any ancestor that lies outside the removed subtree. Those ancestors permanently carry inflated descendant statistics, corrupting the eviction order used when the tx-pool is full.

## Finding Description

`remove_entry_and_descendants` (lines 252–265) first iterates over all removed IDs and calls `remove_entry_links` on each, then calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs lines 252-265
for id in &removed_ids {
    self.remove_entry_links(id);   // tears down link record for every member
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (lines 418–430) calls `self.links.remove(id)` which deletes the entry's record from `TxLinksMap::inner`. When `remove_entry` subsequently calls `update_ancestors_index_key` (line 242), that function calls `self.links.calc_ancestors(&child.proposal_short_id())` (line 434). `calc_ancestors` delegates to `calc_relative_ids` (links.rs lines 37–50), which does `self.inner.get(short_id).map(...).unwrap_or_default()` — returning an empty set because the record is already gone. No ancestor outside the removed batch is ever reached, so `sub_descendant_weight` and the `evict_key` refresh (lines 439–442) are never executed for them.

The developer comment at line 256 — *"update links state for remove, so that we won't update_descendants_index_key in remove_entry"* — confirms the intent was only to suppress the redundant `update_descendants_index_key` call (since all descendants are being removed anyway). The side effect of also silencing `update_ancestors_index_key` for outside ancestors is the defect.

For a chain **A → B → C**, calling `remove_entry_and_descendants(B)`:
1. `remove_entry_links(B)` and `remove_entry_links(C)` are called — both link records deleted.
2. `remove_entry(B)` → `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B)` returns `{}` → A is never reached.
3. A's `descendants_count` stays at 3 (should be 1), `descendants_fee`/`descendants_size`/`descendants_cycles` remain inflated, and `evict_key` is never refreshed.

`EvictKey::cmp` (sort_key.rs lines 92–104) uses `fee_rate` first, then `descendants_count` as a tiebreaker. A stale high `descendants_count` on A causes it to sort as harder to evict than it actually is, so `next_evict_entry` → `iter_by_evict_key()` (lines 380–385) skips A in favour of a different victim.

No corrective code path recomputes or resets the stale `evict_key` after the fact.

## Impact Explanation

This is a suboptimal/incorrect implementation of CKB's tx-pool state management mechanism. The `evict_key` index is a stored, sorted field that directly drives victim selection when the pool reaches capacity. Stale values cause the wrong transaction to be evicted: a low-fee ancestor with artificially inflated `descendants_count` survives rounds it should lose, while a genuinely lower-priority transaction is evicted instead. The corruption is persistent — it lasts until the ancestor is itself removed. This matches **Medium (2001–10000 points): suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation

Any code path that calls `remove_entry_and_descendants` on an entry that has a pool-resident ancestor triggers the bug. This occurs routinely: a committed block double-spending a mid-chain pool entry (`resolve_conflict`), an RBF replacement evicting a mid-chain entry, or pool-size eviction (`limit_size`) targeting a mid-chain entry. An unprivileged submitter can deliberately construct a parent–child chain (A → B → C), then submit a conflicting transaction to evict B and C, leaving A with permanently stale stats. No special privilege is required.

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

Alternatively, pre-compute and store each entry's ancestor set before the first `remove_entry_links` call, then apply `sub_descendant_weight` directly using those snapshots.

## Proof of Concept

1. Build a `PoolMap` and add three transactions: tx1 (root, fee=1000), tx2 (spends tx1 output 0, fee=100), tx3 (spends tx2 output 0, fee=100).
2. Assert tx1's `descendants_count == 3`, `descendants_fee == 1200`.
3. Call `pool_map.remove_entry_and_descendants(&tx2.proposal_short_id())`.
4. Assert tx2 and tx3 are absent from the pool.
5. Retrieve tx1's entry and assert `descendants_count == 1` and `descendants_fee == 1000` — **this assertion will fail**, confirming the stale stats.
6. Call `next_evict_entry` repeatedly while adding many low-fee transactions to fill the pool; observe that tx1 is not selected for eviction despite having zero real descendants, because its stale `evict_key` ranks it as harder to evict.

The existing unit test `test_remove_entry` in `tx-pool/src/component/tests/score_key.rs` only exercises single-entry `remove_entry`, not `remove_entry_and_descendants`, so the defect is not currently caught by the test suite.