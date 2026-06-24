Audit Report

## Title
Stale Descendant Fee Accounting After Batch RBF Removal Enables Pool Eviction Manipulation — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` strips all link graph entries before calling `remove_entry` on each removed transaction. Because `update_ancestors_index_key` resolves ancestors by walking the live link graph, the pre-removal causes it to return an empty ancestor set for every removed entry. Surviving in-pool parents of the removed subtree never have `sub_descendant_weight` called on them, leaving their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` permanently inflated. The `EvictKey` derived from these stale fields causes the eviction index to rank those parents as more valuable than they actually are.

## Finding Description
In `remove_entry_and_descendants` (L252–265), all link entries for the entire removed subtree are stripped first via `remove_entry_links`, then `remove_entry` is called on each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // strips link graph for ALL entries upfront
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) removes the entry from its parents' children sets and then deletes the entry's own link node entirely. When `remove_entry` subsequently calls `update_ancestors_index_key` (L432–445), it calls `self.links.calc_ancestors(&child.proposal_short_id())`, which looks up the child's link node — already deleted — and returns an empty `HashSet`. The loop over ancestors never executes, so `sub_descendant_weight` is never called on any surviving parent, and `e.evict_key = e.inner.as_evict_key()` is never recomputed for them.

The code comment at L256 confirms the intent: *"update links state for remove, so that we won't update_descendants_index_key in remove_entry"* — the goal was to skip updating descendants' ancestor scores (since they are all being removed). The unintended side effect is that `update_ancestors_index_key`, which must update *surviving* ancestors' descendant scores, is also silently broken.

The `EvictKey` is computed directly from the stale cached fields (entry.rs L234–247):
```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

`remove_entry_and_descendants` is called from `process_rbf` (process.rs L203–206) during RBF conflict resolution, making this reachable by any unprivileged submitter when RBF is enabled.

## Impact Explanation
A surviving in-pool parent of an RBF-replaced child retains an inflated `descendants_feerate` in the eviction index. When the pool reaches capacity and `next_evict_entry` runs (pool_map.rs L380–385), that parent is ranked as more valuable than it actually is and skipped in favour of evicting legitimate higher-fee transactions. An attacker can park a near-zero-fee transaction in the pool indefinitely at the one-time cost of a single RBF cycle, causing legitimate transactions to be rejected with `Reject::Full`. This constitutes a pool manipulation primitive that can cause CKB network congestion with few costs — matching the **High** impact class.

## Likelihood Explanation
RBF is enabled whenever `min_rbf_rate > min_fee_rate`, a common operator configuration. The attack requires no special privileges: any unprivileged submitter can craft the three-transaction sequence. The pool-full condition is regularly reached on mainnet during congestion, making the eviction path active. The stale state persists indefinitely until the parent transaction is confirmed or the pool is cleared, so a single RBF cycle is sufficient to achieve the effect.

## Recommendation
Before stripping links in `remove_entry_and_descendants`, iterate over every entry in the removed set and call `update_ancestors_index_key(entry, EntryOp::Remove)` while the link graph is still intact. This ensures surviving ancestors have their `descendants_*` fields and `evict_key` correctly decremented. The pre-removal of links can then proceed as before to suppress the (correctly skippable) `update_descendants_index_key` calls for entries that are themselves being removed.

## Proof of Concept
1. Submit `tx_parent` with fee = 1 shannon/byte (low fee). Pool records `tx_parent.descendants_fee = tx_parent.fee`, `descendants_count = 1`.
2. Submit `tx_child` (child of `tx_parent`) with fee = 10,000 shannons/byte. `record_entry_descendants` calls `add_descendant_weight` on `tx_parent`, so `tx_parent.descendants_fee += tx_child.fee` and `tx_parent.descendants_count = 2`.
3. Submit `tx_new` via RBF replacing `tx_child` (paying the required RBF premium). `tx_new` spends the same input as `tx_child` but does **not** depend on `tx_parent`. `process_rbf` calls `remove_entry_and_descendants(tx_child_id)`.
   - `remove_entry_links(tx_child_id)` removes `tx_child` from `tx_parent`'s children set and deletes `tx_child`'s link node.
   - `remove_entry(tx_child_id)` calls `update_ancestors_index_key`: `calc_ancestors(tx_child_id)` returns `{}` (link node gone) → `sub_descendant_weight` is never called on `tx_parent`.
4. `tx_parent` now has `descendants_fee = tx_child.fee` (stale) and `descendants_count = 2` (stale), yielding `EvictKey.fee_rate ≈ 10,000 shannons/byte` despite having zero real descendants.
5. Fill the pool. The eviction loop skips `tx_parent` (appears high-value) and evicts legitimate transactions instead. `tx_parent` remains in the pool indefinitely at near-zero effective cost after the one-time RBF payment.

A unit test can verify this by asserting that after step 3, `tx_pool.pool_map.get(&tx_parent_id).unwrap().descendants_count == 1` and `descendants_fee == tx_parent.fee` — both assertions will fail against the current code.