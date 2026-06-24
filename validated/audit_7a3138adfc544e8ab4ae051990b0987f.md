Audit Report

## Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Allows Eviction-Priority Inflation — (File: tx-pool/src/component/pool_map.rs)

## Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries for the entire subtree before calling `remove_entry` on each member. Because `remove_entry` relies on `self.links.calc_ancestors` to find which surviving pool entries need their `descendants_*` fields decremented, and those link entries are already gone, the ancestor update is silently skipped. Ancestors of the removed subtree root are left with permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, causing their `EvictKey` to reflect a falsely high fee rate. An unprivileged submitter can exploit this via RBF or conflict removal to protect low-fee transactions from eviction and displace legitimate high-fee transactions, causing mempool congestion at low cost.

## Finding Description

**Root cause — pre-removal of links breaks ancestor accounting:**

`remove_entry_and_descendants` (L252–265) first calls `remove_entry_links` for every ID in the subtree, then calls `remove_entry` on each:

```rust
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
for id in &removed_ids {
    self.remove_entry_links(id);   // strips ALL link entries first
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) calls `self.links.remove(id)`, which deletes the entry from `TxLinksMap::inner`. It also removes the ID from its parents' children sets, severing the upward link.

Inside `remove_entry` (L235–250), the first operation is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` (L432–445) calls:

```rust
let ancestors: HashSet<ProposalShortId> =
    self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)`. Because `remove_entry_links` already called `self.links.remove(id)`, `self.inner.get(short_id)` returns `None`, `direct` is an empty set, and `calc_relation_ids` returns an empty set. The loop over ancestors executes zero iterations. `sub_descendant_weight` is never called on any surviving ancestor.

The comment in the code ("so that we won't update_descendants_index_key in remove_entry") reveals the intent was only to skip updating descendants' `ancestors_*` fields (correct, since those entries are being removed anyway), but the side effect is also skipping the update of ancestors' `descendants_*` fields (incorrect, since those entries remain in the pool).

**Stale fields:**

After `remove_entry_and_descendants(tx2_id)` in a chain `tx1 → tx2 → tx3`:
- `tx1.descendants_fee` remains at `fee(tx1)+fee(tx2)+fee(tx3)` instead of `fee(tx1)`
- `tx1.descendants_count` remains `3` instead of `1`
- `tx1.descendants_size` and `tx1.descendants_cycles` are similarly inflated

**Existing test is insufficient:**

`test_remove_entry_and_descendants` (score_key.rs L170–230) only asserts that tx2 and tx3 are absent and that `calc_descendants` returns an empty set. It never reads `tx1.descendants_fee`, `tx1.descendants_count`, or `tx1.evict_key` after the removal, so the stale accounting goes undetected.

**Exploit flow:**

1. Submit `tx1` (fee=100, size=100). `tx1.descendants_fee = 100`.
2. Submit `tx2` spending `tx1`'s output (fee=200, size=200). `tx1.descendants_fee = 300`.
3. Submit `tx3` spending `tx2`'s output (fee=200, size=200). `tx1.descendants_fee = 500`.
4. Trigger `remove_entry_and_descendants(tx2_id)` via RBF (submit `tx2'` with higher fee) or conflict. Links for tx2 and tx3 are pre-removed; `calc_ancestors` returns empty; `tx1.descendants_fee` stays at `500`.
5. `tx2'` is added; `add_descendant_weight` is called on `tx1`: `tx1.descendants_fee = 700`.
6. Repeat steps 4–5 to amplify `tx1.descendants_fee` without bound.

**Why existing guards do not prevent this:**

There are no guards that re-validate or recompute `descendants_*` fields after removal. The `EvictKey` is computed lazily from the stored (now stale) fields. No integrity check exists between the live link graph and the stored descendant accounting.

## Impact Explanation

The `EvictKey` for every surviving ancestor is computed directly from the stale fields (entry.rs L234–247):

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

`next_evict_entry` selects the entry with the lowest `EvictKey` for eviction. With inflated `descendants_feerate`, `tx1` is ranked as more valuable than it truly is and is evicted last. When the pool is full and `limit_size` runs, legitimate high-fee transactions submitted by other users are evicted in place of the attacker's low-fee `tx1`. By repeating the cycle, the attacker can make `tx1`'s apparent fee rate grow without bound, permanently shielding it from eviction regardless of pool pressure.

This constitutes **mempool congestion at low cost**: the attacker occupies pool space with a low-fee transaction indefinitely, preventing legitimate transactions from entering, which maps to the allowed impact: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

The attack requires only the ability to submit transactions to the tx-pool, which is available to any unprivileged peer or RPC caller. Two concrete, low-cost trigger paths exist:

1. **RBF path**: Submit `tx2`, then replace it with `tx2'` at a slightly higher fee. `process_rbf` (process.rs L203–205) calls `remove_entry_and_descendants` on the old child. Each replacement cycle costs only the incremental RBF fee bump while adding another copy of the child's fee to the ancestor's stale `descendants_fee`.

2. **Conflict path**: Submit a second transaction spending the same input as `tx2`. `resolve_conflict` (pool_map.rs L309–310) calls `remove_entry_and_descendants` on `tx2`, leaving `tx1`'s accounting inflated at zero additional cost to the attacker.

The attack is repeatable, deterministic, and requires no special privileges, timing, or network conditions.

## Recommendation

Before the link-removal loop in `remove_entry_and_descendants`, collect the ancestors of the subtree root (those that will survive) and call `sub_descendant_weight` on each of them for every entry being removed. Concretely:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors' descendants_* fields BEFORE tearing down links
    for removed_id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(removed_id).map(|e| e.inner.clone()) {
            self.update_ancestors_index_key(&entry, EntryOp::Remove);
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Alternatively, restructure `remove_entry` so that ancestor updates use a snapshot of the link graph taken before any links are torn down. Also add a test assertion that checks `tx1.descendants_fee`, `tx1.descendants_count`, and `tx1.evict_key` after `remove_entry_and_descendants` to prevent regression.

## Proof of Concept

The existing test `test_remove_entry_and_descendants` (score_key.rs L170–230) can be extended to expose the bug by adding these assertions after `map.remove_entry_and_descendants(&tx2_id)`:

```rust
// After removing tx2 and tx3, tx1 should have only itself as a descendant
let tx1_entry = map.get(&tx1_id).expect("tx1 must still exist");
assert_eq!(tx1_entry.descendants_count, 1,   // FAILS: returns 3
    "descendants_count should be 1 after removing tx2 and tx3");
assert_eq!(tx1_entry.descendants_fee, Capacity::shannons(100),  // FAILS: returns 500
    "descendants_fee should equal tx1's own fee only");
assert_eq!(tx1_entry.descendants_size, 100,  // FAILS: returns 500
    "descendants_size should equal tx1's own size only");
```

To demonstrate unbounded amplification, add a second child `tx2'` after the removal and assert that `tx1.descendants_fee` equals `100 + 200 = 300` (the correct value). Without the fix, it will equal `500 + 200 = 700`, confirming the additive inflation.