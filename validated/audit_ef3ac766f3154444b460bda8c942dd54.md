Audit Report

## Title
Stale `descendants_*` Accounting in `PoolMap::remove_entry_and_descendants` Allows Ancestor Eviction Priority Inflation — (`tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` clears all link records for every entry in the removed subtree before calling `remove_entry` on each. Because `update_ancestors_index_key` relies on those link records to find still-live ancestors, the pre-clearing step silently prevents any ancestor from receiving a `sub_descendant_weight` call. Ancestors of the removed root transaction permanently retain inflated `descendants_size`, `descendants_cycles`, `descendants_fee`, and `descendants_count` values. An unprivileged submitter can exploit this via repeated RBF replacements to make a low-fee transaction appear more valuable than it is, preventing it from being evicted when the pool is full.

## Finding Description

`remove_entry_and_descendants` first calls `remove_entry_links` for every entry in the subtree, then calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs L252-265
for id in &removed_ids {
    self.remove_entry_links(id);   // links cleared for ALL removed entries
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` removes the entry from its parents' children sets, removes all parents from the entry's parents set, and then removes the entry's record from `self.links` entirely:

```rust
// L418-430
fn remove_entry_links(&mut self, id: &ProposalShortId) {
    if let Some(parents) = self.links.get_parents(id).cloned() {
        for parent in parents { self.links.remove_child(&parent, id); }
    }
    ...
    self.links.remove(id);
}
```

Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`:

```rust
// L242
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

That function calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because the entry's link record was already removed by the pre-loop, `calc_ancestors` returns an empty set — no ancestor receives `sub_descendant_weight`. The comment in the source (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) acknowledges the intent to skip descendant-side updates, but the side-effect of also skipping ancestor-side updates is not addressed.

By contrast, when `remove_entry` is called directly (not via `remove_entry_and_descendants`), `remove_entry_links` is called *after* `update_ancestors_index_key` (L242 before L245), so ancestors are correctly updated.

The `descendants_*` fields feed directly into `EvictKey`:

```rust
// entry.rs L234-247
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
let feerate = FeeRate::calculate(entry.fee, weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

An inflated `descendants_fee` raises `descendants_feerate` above the entry's true fee rate, making the entry appear more valuable and less likely to be evicted by `limit_size`.

Each RBF replacement of a descendant compounds the inflation: the old descendant's contribution is never subtracted, and the new descendant's contribution is added on top via `update_ancestors_index_key(..., EntryOp::Add)` when the replacement is inserted.

## Impact Explanation

This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

A low-fee transaction can be kept alive indefinitely in a full pool by repeatedly submitting and RBF-replacing a high-fee child. Each cycle adds to the ancestor's apparent descendant weight without ever subtracting the replaced child's contribution. When `limit_size` runs, `next_evict_entry` selects victims by lowest `EvictKey`; the artificially inflated ancestor is spared while honest high-fee transactions may be rejected. The attacker pays a bounded, predictable RBF fee premium per replacement, while the benefit (pool slot retention for a low-fee transaction) persists indefinitely and compounds.

## Likelihood Explanation

RBF is enabled by default in the mainnet configuration (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`). Any unprivileged tx-pool submitter can trigger this path by submitting a parent transaction and then repeatedly submitting and RBF-replacing a child transaction. No special privileges, leaked keys, or victim mistakes are required. The exploit is deterministic and repeatable.

## Recommendation

Before clearing link records in `remove_entry_and_descendants`, update the ancestors of the root entry being removed. Specifically, call `update_ancestors_index_key(&root_entry, EntryOp::Remove)` while the root entry's links are still intact, so that every still-live ancestor receives a correct `sub_descendant_weight` call. The descendants of the root (which are also being removed) do not need ancestor updates since they are all being expelled, so the pre-clearing of their links is safe — only the root's own ancestor update must happen first.

Alternatively, restructure `remove_entry_and_descendants` to call `remove_entry` on the root entry first (which correctly updates ancestors via the existing `remove_entry` path), then remove the remaining descendants.

## Proof of Concept

1. Submit `tx_A` (low fee rate, just above `min_fee_rate`) to the pool.
2. Submit `tx_B` (child of `tx_A`, very high fee rate). `tx_A.descendants_fee` now includes `tx_B.fee`; `tx_A.EvictKey.fee_rate` reflects `tx_B`'s high rate.
3. Submit `tx_B'` (same inputs as `tx_B`, fee satisfying RBF rules) to trigger `process_rbf`:
   - `remove_entry_and_descendants(tx_B)` is called.
   - `remove_entry_links(tx_B)` runs: `tx_B` is removed from `self.links`; `tx_A`'s children set no longer contains `tx_B`.
   - `remove_entry(tx_B)` runs: `update_ancestors_index_key(tx_B, Remove)` calls `calc_ancestors(tx_B)` → empty set (links gone) → `tx_A.descendants_*` is **not decremented**.
   - `tx_B'` is inserted: `update_ancestors_index_key(tx_B', Add)` correctly increments `tx_A.descendants_*`.
   - `tx_A.descendants_fee` now equals `tx_A.fee + tx_B.fee + tx_B'.fee` — doubly inflated.
4. Repeat step 3 N times. After N replacements, `tx_A.descendants_fee ≈ tx_A.fee + N × tx_B_fee + tx_B'_fee`, making `tx_A` effectively immune to eviction by `limit_size`.
5. Fill the pool with other transactions to trigger `limit_size`. Observe that `tx_A` is not selected for eviction despite its low intrinsic fee rate, while honest high-fee transactions are rejected.

A unit test can be written against `PoolMap` directly: add `tx_A` and `tx_B` (child), call `remove_entry_and_descendants(tx_B_id)`, then assert that `pool_map.get(tx_A_id).descendants_count == 1` and `descendants_fee == tx_A.fee`. Without the fix, `descendants_count` will remain 2 and `descendants_fee` will remain inflated.