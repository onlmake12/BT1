Audit Report

## Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Score — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` strips all link records for every entry being removed in a pre-pass before calling `remove_entry` on each one. Because `update_ancestors_index_key` resolves ancestors through those same link records, it always finds an empty ancestor set and never calls `sub_descendant_weight` on ancestor transactions that remain in the pool. Those ancestors permanently retain inflated `descendants_fee / descendants_size / descendants_cycles / descendants_count` fields, corrupting the `EvictKey` used to rank transactions for eviction.

## Finding Description

In `remove_entry_and_descendants` (lines 252–264), all link records are torn down first via `remove_entry_links` before `remove_entry` is called on each collected id:

```rust
// tx-pool/src/component/pool_map.rs lines 252-264
for id in &removed_ids {
    self.remove_entry_links(id);   // ALL links removed here
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (lines 418–430) removes the entry from its parents' children sets, removes the entry from its children's parents sets, and deletes the entry from `self.links` entirely. After this pre-pass, no link information survives for any of the removed entries.

Inside `remove_entry` (lines 235–250), `update_ancestors_index_key` is called:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` (lines 432–445) resolves ancestors via `self.links.calc_ancestors(...)`. Since all links were already removed, `calc_ancestors` returns an empty set for every entry being processed. `sub_descendant_weight` is never called on any ancestor that remains in the pool.

The existing test `test_remove_entry_and_descendants` (lines 170–230 of `score_key.rs`) only asserts that tx2 and tx3 are absent and that tx1's descendant set is empty — it never checks tx1's `descendants_count`, `descendants_fee`, `descendants_size`, or `descendants_cycles` after the call, so the bug is not caught.

By contrast, `remove_entry` called directly (not through `remove_entry_and_descendants`) works correctly because links are still intact at the time `update_ancestors_index_key` runs, as verified by the test at lines 157–167 of `score_key.rs`.

The three production callers of `remove_entry_and_descendants` — `resolve_conflict` (line 310, 321), `resolve_conflict_header_dep`, and `check_and_record_ancestors` — are all reachable by unprivileged users.

## Impact Explanation

`EvictKey` is computed directly from the stale fields (entry.rs lines 234–247):

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

An ancestor whose removed descendants had a higher fee rate than itself retains an inflated `fee_rate` in its `EvictKey`. When the pool is full and `next_evict_entry` selects the lowest-`EvictKey` entry to drop, that ancestor is ranked as more valuable than it truly is. Legitimate transactions with true fee rates between the ancestor's actual and inflated rates are evicted in its place. An attacker can repeat this pattern at low cost — paying only the fee for a conflicting transaction each iteration — to accumulate many low-fee transactions that resist eviction, displacing legitimate higher-fee transactions and causing pool congestion. This matches: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The trigger requires no privileged access: submit a low-fee root transaction X, submit high-fee-rate descendants A → B spending X's output, then submit a conflicting transaction A′ spending the same input as A. `resolve_conflict` calls `remove_entry_and_descendants(A)`, leaving X with permanently inflated descendant stats. The cost per iteration is one conflicting transaction fee. The attack is repeatable, deterministic, and requires no majority hash power or social engineering.

## Recommendation

Collect and apply ancestor updates **before** tearing down links, skipping ancestors that are themselves in the removal set:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();
    for removed_id in &removed_ids {
        if let Some(entry) = self.get(removed_id).cloned() {
            let ancestors = self.links.calc_ancestors(removed_id);
            for anc_id in ancestors.difference(&removed_set) {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&entry);
                    e.evict_key = e.inner.as_evict_key();
                });
            }
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Add a regression test asserting that after `remove_entry_and_descendants` on a child chain, the surviving ancestor's `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` equal the ancestor's own values (i.e., are fully decremented).

## Proof of Concept

**Setup**: pool contains chain `X → A → B`.

| Tx | fee | size | cycles |
|---|---|---|---|
| X | 100 | 100 | 100 |
| A | 300 | 200 | 200 |
| B | 200 | 200 | 200 |

After insertion, X's tracked state: `descendants_fee=600, descendants_size=500, descendants_cycles=500, descendants_count=3`.

**Trigger**: submit tx A′ spending the same input as A. `resolve_conflict` calls `remove_entry_and_descendants(A)`.

**Expected X state**: `descendants_fee=100, descendants_size=100, descendants_cycles=100, descendants_count=1`.

**Actual X state (bug)**: `descendants_fee=600, descendants_size=500, descendants_cycles=500, descendants_count=3`.

X's `EvictKey.fee_rate` is computed as `600/500 = 1.2 shannons/KW` instead of the correct `100/100 = 1.0 shannons/KW`. Any transaction with a true fee rate in `[1.0, 1.2)` is evicted before X when the pool is full. The existing `test_remove_entry_and_descendants` test (lines 224–229 of `score_key.rs`) does not assert on X's descendant-weight fields and therefore does not catch this regression.