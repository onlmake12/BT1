The code is confirmed. Let me verify the `EvictKey` ordering direction and the `next_evict_entry` iteration to confirm the impact direction.

Audit Report

## Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Corrupts Eviction Ordering — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` calls `remove_entry_links` on every entry in the removal batch — including the root — before iterating to call `remove_entry`. Because the root's link record is erased first, the subsequent `update_ancestors_index_key` call inside `remove_entry` resolves an empty ancestor set and never decrements `descendants_count / descendants_size / descendants_cycles / descendants_fee` on surviving ancestors. Those ancestors retain permanently inflated `evict_key` values, causing them to survive pool eviction rounds they should lose and displacing legitimate higher-fee transactions.

## Finding Description
`remove_entry_and_descendants` (L252–265) first collects the root id and all descendant ids, then calls `remove_entry_links` on **all** of them in a single loop before any `remove_entry` call:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // erases root's parent pointers too
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) calls `self.links.remove(id)`, which deletes the entry from `TxLinksMap::inner`. When `remove_entry(root_id)` is subsequently called (L235–250), it invokes `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` (L242). That function calls `self.links.calc_ancestors(&child.proposal_short_id())` (L433–434), which resolves via `calc_relative_ids` (links.rs L37–50): `self.inner.get(root_id)` returns `None` (root was already removed), so `direct` is `unwrap_or_default()` — an empty set — and `calc_relation_ids` returns an empty set. The `for anc_id in &ancestors` loop body never executes; `sub_descendant_weight` and `evict_key` recomputation are silently skipped for every surviving ancestor.

The comment at L256 acknowledges the pre-removal is intentional to suppress `update_descendants_index_key` for the removed descendants, but it also inadvertently suppresses the necessary ancestor update for entries that **remain** in the pool. The global `update_stat_for_remove_tx` (L247) correctly adjusts `total_tx_size` / `total_tx_cycles`, but per-entry descendant fields are never decremented.

`EvictKey` is computed as (entry.rs L234–247):
```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
let feerate = FeeRate::calculate(entry.fee, weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```
`next_evict_entry` (L380–385) iterates `iter_by_evict_key()` in ascending order, evicting the entry with the **lowest** `fee_rate` first. An ancestor with inflated `descendants_fee` / `descendants_size` has an artificially high `fee_rate` in its `EvictKey`, so it is evicted later than it should be.

## Impact Explanation
Surviving ancestors of a removed root retain inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`. These feed directly into `EvictKey.fee_rate` via `as_evict_key()`. An attacker can make a low-fee parent transaction appear to carry the fee rate of a high-fee child that no longer exists, causing the parent to survive eviction rounds it should lose. Repeated triggering accumulates unbounded inflation across multiple pool entries. When the pool reaches `max_tx_pool_size`, legitimate higher-fee transactions are evicted in place of the attacker's inflated low-fee entries. This constitutes a low-cost, repeatable mechanism to cause CKB tx-pool congestion and degrade transaction throughput for honest users — matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation
The trigger is `resolve_conflict` (L305–332), called whenever a submitted transaction spends an input already claimed by a pool transaction. Any unprivileged RPC caller or P2P peer can reach this path via `send_transaction` or the relay protocol. The attacker requires no key material, no mining power, and no special privilege. The three-step sequence (submit parent → submit child → submit conflicting tx) is cheap and repeatable. Each iteration permanently inflates one or more ancestors' evict keys for the lifetime of those entries. The cost per inflation event is the fee of the conflicting transaction X, which can be set to the minimum relay fee.

## Recommendation
Before erasing the root entry's links, snapshot its ancestor set and update those ancestors' descendant weights. The fix should be applied inside `remove_entry_and_descendants`:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors BEFORE links are erased
    if let Some(root_entry) = self.get(id).cloned() {
        self.update_ancestors_index_key(&root_entry, EntryOp::Remove);
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

Alternatively, save the ancestor set via `self.links.calc_ancestors(id)` before the links loop and manually call `sub_descendant_weight` + `evict_key` recomputation on each surviving ancestor.

## Proof of Concept
1. Build a `PoolMap` and add parent transaction **P** (low fee, e.g. 100 shannons, size 100).
2. Add child **C1** spending P's output (high fee, e.g. 10 000 shannons, size 100). P's `descendants_fee` becomes 10 100, `descendants_count` = 2.
3. Add child **C2** also spending P's output (any fee). P's `descendants_count` = 3.
4. Call `pool_map.remove_entry_and_descendants(&C1_id)` (simulating `resolve_conflict` when a conflicting tx spends C1's input).
5. Assert: `pool_map.get(&P_id).unwrap().descendants_count` should equal 1 (only P itself) but equals 3 — **stale**.
6. Assert: `pool_map.get(&P_id).unwrap().descendants_fee` should equal P.fee but retains C1.fee + C2.fee — **inflated**.
7. Assert: `pool_map.get(&P_id).unwrap().evict_key.fee_rate` reflects the inflated descendants_feerate rather than P's own fee rate.
8. Fill the pool to capacity and observe that P is not evicted despite having the lowest real fee rate, while a legitimate transaction with a higher real fee rate is evicted instead.