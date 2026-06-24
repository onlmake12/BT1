Having read both files in full, I can now verify each claim in the submitted report against the actual code.

Audit Report

## Title
`remove_entry_and_descendants` Silently Skips Ancestor `evict_key` Update After Link Teardown — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` tears down every entry's link record via `remove_entry_links` before calling `remove_entry`. Because `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, and that lookup returns an empty set once the link record is gone, no surviving ancestor ever has its `descendants_*` fields or `evict_key` corrected. Ancestors that remain in the pool permanently carry inflated effective fee-rate values, corrupting the eviction-priority index and allowing low-fee transactions to resist eviction indefinitely.

## Finding Description
`remove_entry_and_descendants` (pool_map.rs L252-265) first iterates all removed IDs and calls `remove_entry_links` for each (L257-259). `remove_entry_links` (L418-430) calls `self.links.remove(id)` at L429, which removes the entry from `self.links.inner` (links.rs L94-96). Only after all links are torn down does the function call `remove_entry` for each ID (L261-264).

`remove_entry` (L235-250) calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` at L242. That function (L432-445) calls `self.links.calc_ancestors(&child.proposal_short_id())` at L434, which resolves to `calc_relative_ids` (links.rs L37-50). `calc_relative_ids` does `self.inner.get(short_id)` at L44 — returns `None` because `remove_entry_links` already called `self.links.remove(id)`. The result is `unwrap_or_default()` — an empty `HashSet`. The ancestor update loop at L435-444 never executes.

The code comment at L256 ("update links state for remove, so that we won't update_descendants_index_key in remove_entry") confirms the intent was to skip descendant updates (correct, since descendants are also being removed), but the side effect is that `update_ancestors_index_key` is also silently neutered.

Consequently, for every ancestor T1 of a removed transaction T2:
- `T1.inner.descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count` retain T2's contribution (entry.rs L35-41)
- `T1.evict_key` (derived via `EvictKey::from(&TxEntry)` at entry.rs L234-247) computes `descendants_feerate` from the stale `descendants_fee` and `descendants_weight`, overstating T1's effective fee rate

`next_evict_entry` (L380-385) iterates `iter_by_evict_key()` in ascending order to find the cheapest transaction to evict. T1's inflated `evict_key` places it later in that order than its true fee rate warrants, so it is never selected for eviction.

The two reachable call sites are `resolve_conflict` (L305-332) and `resolve_conflict_header_dep` (L267-292), both triggered by ordinary transaction submission.

## Impact Explanation
This matches **High (10001–15000 points): Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

An attacker can systematically fill the mempool with low-fee parent transactions whose `evict_key` permanently reflects a removed high-fee child. Once the pool is full of such stale-keyed entries, `next_evict_entry` cannot identify them as eviction candidates because their apparent fee rate is artificially high. Legitimate higher-fee transactions are rejected. The pool becomes a sink for low-fee dust that cannot be displaced, degrading block-template quality and blocking fee-paying users across any targeted node.

## Likelihood Explanation
No privileged access, key material, or majority hash power is required. Any node-connected peer can execute the attack:
1. Submit T1 (low fee) — accepted.
2. Submit T2 (high-fee child of T1) — accepted; T1's `evict_key` is updated upward.
3. Submit T3 conflicting with T2 — `resolve_conflict` calls `remove_entry_and_descendants(T2)`; T2 is removed but T1's `evict_key` is not corrected.
4. T1 now occupies a pool slot with an inflated `evict_key` and will not be evicted.
5. Repeat with fresh key pairs.

The cost per captured slot is only T1's fee plus T3's minimum-acceptance fee; T2 is never mined so its fee is never paid. The attack scales linearly with pool capacity and is fully repeatable.

## Recommendation
Collect the ancestors of the root entry **before** any link teardown, then after all removals are complete, recompute and apply the correct `evict_key` for each surviving ancestor:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    // Snapshot ancestors BEFORE links are torn down
    let ancestors_to_fix: HashSet<ProposalShortId> = self.links.calc_ancestors(id);

    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Recompute evict_key for each ancestor still in the pool
    for anc_id in &ancestors_to_fix {
        self.entries.modify_by_id(anc_id, |e| {
            for r in &removed {
                e.inner.sub_descendant_weight(r);
            }
            e.evict_key = e.inner.as_evict_key();
        });
    }

    removed
}
```

Alternatively, restructure removal so that `remove_entry_links` for the root is called **after** `update_ancestors_index_key` has already run for it, preserving the existing incremental logic.

## Proof of Concept
**Minimal manual steps:**

1. Start a CKB node with a small mempool (e.g., `max_tx_pool_size = 10`).
2. Submit T1 with fee rate 1 shannon/byte (low).
3. Submit T2 spending an output of T1 with fee rate 1000 shannon/byte (high). Observe T1's `evict_key` rises.
4. Submit T3 spending the same input as T2 (double-spend). `resolve_conflict` removes T2.
5. Inspect T1's `evict_key` via RPC or debug log — it still reflects T2's fee contribution.
6. Fill the remaining pool slots with normal transactions.
7. Submit a new transaction with fee rate 500 shannon/byte — it is rejected despite having a higher true fee rate than T1, because T1's stale `evict_key` prevents its eviction.

**Invariant/fuzz test plan:** After any call to `remove_entry_and_descendants`, assert for every surviving entry E: `E.evict_key == EvictKey::from(&E.inner)` and `E.inner.descendants_count == 1 + |actual_descendants_in_pool(E)|`. The current code violates both invariants for ancestors of removed entries.