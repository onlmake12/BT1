Audit Report

## Title
Stale `descendants_*` State in Ancestor `TxEntry` After `remove_entry_and_descendants` — (File: tx-pool/src/component/pool_map.rs)

## Summary
`remove_entry_and_descendants` tears down all parent-child links via `remove_entry_links` in a first pass before calling `remove_entry` on each node. Because `update_ancestors_index_key` inside `remove_entry` relies on `calc_ancestors` traversing those links, it silently returns an empty set and never decrements the ancestor's `descendants_fee`, `descendants_size`, `descendants_cycles`, or `descendants_count`. The ancestor's `EvictKey` is computed from these stale inflated values, causing `limit_size` to skip the ancestor during eviction and instead evict legitimate higher-fee transactions submitted by other users.

## Finding Description

**Root cause — links torn down before ancestor update**

In `remove_entry_and_descendants` (pool_map.rs L252–265), all entries in the subtree have their links removed via `remove_entry_links` in a first pass, then `remove_entry` is called on each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // severs ALL parent↔child edges
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) calls `self.links.remove_child(&parent, id)`, `self.links.remove_parent(&child, id)`, and `self.links.remove(id)` — completely erasing the entry from `TxLinksMap`.

**`update_ancestors_index_key` becomes a no-op**

Inside `remove_entry` (L235–250), `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` is called. This function (L432–445) calls `self.links.calc_ancestors(&child.proposal_short_id())`. `calc_ancestors` delegates to `calc_relative_ids` (links.rs L37–50):

```rust
let direct = self
    .inner
    .get(short_id)
    .map(|link| link.get_direct_ids(relation))
    .cloned()
    .unwrap_or_default();   // returns empty HashSet when entry absent
self.calc_relation_ids(direct, relation)
```

Because `remove_entry_links` already called `self.links.remove(id)`, `self.inner.get(short_id)` returns `None`, `direct` is an empty `HashSet`, and `calc_relation_ids` returns an empty set. The loop body in `update_ancestors_index_key` never executes. The ancestor's `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and `evict_key` are never decremented.

**Stale `EvictKey` corrupts eviction ordering**

`EvictKey` is derived directly from `descendants_fee` and `descendants_cycles` (entry.rs L234–247):

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

`EvictKey` ordering (sort_key.rs L92–103) sorts ascending by `fee_rate`. `next_evict_entry` (pool_map.rs L380–385) iterates `iter_by_evict_key()` and returns the first (lowest) match. An ancestor with an inflated `EvictKey.fee_rate` sorts later in the iteration and is skipped; a legitimate higher-fee transaction is evicted instead.

**Stale state accumulates on re-addition**

When a new child is added to the same ancestor, `add_descendant_weight` increments on top of the already-inflated counters, compounding the error indefinitely.

**Existing test does not catch this**

The test at score_key.rs L170–230 only asserts that tx2/tx3 are absent and `calc_descendants(tx1)` is empty. It never checks `tx1.descendants_fee`, `tx1.descendants_count`, or `tx1.evict_key` after removal.

## Impact Explanation

The bug allows an unprivileged attacker to permanently occupy pool space with low-fee transactions by inflating the `EvictKey` of any ancestor they control. Legitimate high-fee transactions submitted by other users are evicted instead. This degrades miner revenue and constitutes a denial-of-service against other users' transactions with negligible cost (two transactions and one conflicting transaction per cycle). This matches the allowed CKB bounty impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

No special privilege is required. Any node that can submit transactions to the tx-pool can trigger this. `remove_entry_and_descendants` is reachable from `resolve_conflict` (any conflicting tx submission), `resolve_conflict_header_dep` (block relay), `limit_size` (pool pressure), and `check_and_record_ancestors` (ancestor-count eviction). The attack requires only two transactions and one conflicting transaction — a trivial setup that can be repeated indefinitely to re-inflate after any natural decay.

## Recommendation

Before the `remove_entry_links` loop, collect the ancestors of the root entry and call `sub_descendant_weight` for each entry in the subtree being removed:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update ancestors BEFORE links are torn down
    let ancestors = self.links.calc_ancestors(id);
    for removed_id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(removed_id) {
            let inner = entry.inner.clone();
            for anc_id in &ancestors {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&inner);
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

Add a regression test asserting that after `remove_entry_and_descendants(&tx2_id)`, `tx1.descendants_fee == tx1.fee`, `tx1.descendants_count == 1`, and `tx1.evict_key` reflects only `tx1`'s own fee rate.

## Proof of Concept

```
Pool state:
  tx1 (fee=100) → tx2 (fee=500) → tx3 (fee=200)

After add_proposed(tx1, tx2, tx3):
  tx1.descendants_fee = 800, tx1.evict_key.fee_rate = high

Attacker submits tx2' double-spending tx2's input.
resolve_conflict → remove_entry_and_descendants(tx2_id)

Expected: tx1.descendants_fee = 100, tx1.evict_key.fee_rate = low
Actual:   tx1.descendants_fee = 800 (stale), tx1.evict_key.fee_rate = high (stale)

Pool fills. limit_size → next_evict_entry skips tx1 (high EvictKey).
Legitimate high-fee tx from another user is evicted.

Attacker re-submits tx2 (fee=500) as new child of tx1.
tx1.descendants_fee = 800 + 500 = 1300. Repeat indefinitely.
```

The existing test at `tx-pool/src/component/tests/score_key.rs` L170–230 confirms the setup is valid but does not assert `tx1.descendants_fee` after removal, leaving the bug undetected.