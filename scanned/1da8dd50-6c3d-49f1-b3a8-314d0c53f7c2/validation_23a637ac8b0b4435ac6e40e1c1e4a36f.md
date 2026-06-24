Audit Report

## Title
Stale `descendants_*` State in Ancestor `TxEntry` After `remove_entry_and_descendants` — (File: tx-pool/src/component/pool_map.rs)

## Summary
`remove_entry_and_descendants` tears down all parent-child links via `remove_entry_links` before calling `remove_entry` on each node. Because `update_ancestors_index_key` relies on those links to find ancestor entries, it silently becomes a no-op for every removal in the subtree. Any ancestor that remains in the pool retains inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and a stale `EvictKey`, causing the pool's eviction logic to incorrectly skip that ancestor and instead evict legitimate higher-fee transactions.

## Finding Description

**Root cause — links torn down before ancestor update**

In `remove_entry_and_descendants` (pool_map.rs L252–265), all entries in the subtree — including the root — have their links removed via `remove_entry_links` in a first pass. Only then is `remove_entry` called for each node.

Inside `remove_entry` (L235–250), `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` is called (L242). That function (L432–445) calls `self.links.calc_ancestors(&child.proposal_short_id())` to find which entries to decrement. Because `remove_entry_links` already severed the parent→child edge (e.g., the tx1→tx2 link), `calc_ancestors` returns an empty set. The loop body never executes. `tx1.descendants_count`, `tx1.descendants_size`, `tx1.descendants_cycles`, `tx1.descendants_fee`, and `tx1.evict_key` are never decremented.

The comment at L256 acknowledges the pre-removal of links is intentional to suppress `update_descendants_index_key`, but it equally suppresses `update_ancestors_index_key`, which is the unintended side effect.

**Stale EvictKey propagates to eviction**

`EvictKey` is computed from the stale fields (entry.rs L234–247): `descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight)` and `fee_rate: descendants_feerate.max(feerate)`. An ancestor with inflated `descendants_fee` gets an artificially high `EvictKey.fee_rate`.

`next_evict_entry` (pool_map.rs L380–385) iterates by `EvictKey` ascending to find the cheapest entry to evict. The stale ancestor is skipped. `limit_size` (pool.rs L292–329) calls `next_evict_entry` in a loop until pool size is within bounds, so legitimate high-fee transactions are evicted in its place.

**Stale state compounds on re-addition**

When a new child is added to the same ancestor, `add_descendant_weight` accumulates on top of the already-inflated counters, compounding the error indefinitely.

**Existing test does not catch this**

The test at score_key.rs L170–230 only asserts that tx2/tx3 are absent and that `calc_descendants(tx1)` is empty. It never checks `tx1.descendants_fee`, `tx1.descendants_count`, or `tx1.evict_key` after removal.

## Impact Explanation

An unprivileged attacker can permanently inflate the `EvictKey` of any ancestor transaction they control, causing the pool's eviction logic to skip that ancestor and evict legitimate higher-fee transactions submitted by other users. This degrades miner revenue and constitutes a low-cost, repeatable denial-of-service against other users' transactions — matching the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**.

## Likelihood Explanation

No special privilege is required. Any node that can submit transactions to the tx-pool can trigger this. The attack requires only three transactions (tx_A, tx_B as child, tx_C conflicting with tx_B). `remove_entry_and_descendants` is reachable from `resolve_conflict` (triggered by any conflicting submission), `resolve_conflict_header_dep` (triggered by block relay), `limit_size` (triggered by pool pressure), and `remove_by_detached_proposal` (triggered by normal chain operation). The stale state persists until the ancestor is itself removed or the pool is cleared, and the attacker can re-inflate after any natural decay by repeating steps 2–4.

## Recommendation

Before the `remove_entry_links` loop in `remove_entry_and_descendants`, collect the ancestors of the root entry and call `sub_descendant_weight` for each entry in the subtree being removed:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors before links are torn down
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
    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

Add a regression test asserting `tx1.descendants_fee == tx1.fee` and `tx1.descendants_count == 0` after `remove_entry_and_descendants(&tx2_id)`.

## Proof of Concept

```
Pool state:
  tx1 (fee=100) → tx2 (fee=500) → tx3 (fee=200)

After add_proposed(tx1), add_proposed(tx2), add_proposed(tx3):
  tx1.descendants_fee = 800, tx1.evict_key.fee_rate = high

Attacker submits tx2' conflicting with tx2.
resolve_conflict → remove_entry_and_descendants(tx2_id)
  remove_entry_links(tx2), remove_entry_links(tx3)  ← severs tx1→tx2 link
  remove_entry(tx2): calc_ancestors(tx2) = ∅ → no decrement
  remove_entry(tx3): calc_ancestors(tx3) = ∅ → no decrement

Actual state of tx1 after removal:
  tx1.descendants_fee = 800  (should be 100)
  tx1.evict_key.fee_rate = high (should be low)

Pool fills. limit_size → next_evict_entry → iter_by_evict_key skips tx1.
Legitimate high-fee tx from another user is evicted instead.

Attacker re-submits tx2 (fee=500) as new child of tx1:
  tx1.descendants_fee = 800 + 500 = 1300  (accumulates on stale base)
Repeat indefinitely.
```

The existing test at `tx-pool/src/component/tests/score_key.rs:170` confirms the setup is valid but does not assert `tx1.descendants_fee` after removal, leaving the bug undetected.