Audit Report

## Title
Stale Descendant Weight Tracking After `remove_entry_and_descendants` Causes Incorrect Pool Eviction Ordering — (File: tx-pool/src/component/pool_map.rs)

## Summary
`PoolMap::remove_entry_and_descendants` strips all parent/child links for every entry in the removal batch before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors through the now-cleared link graph, surviving ancestors of the removed subtree never have their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` decremented. Those ancestors retain permanently inflated `EvictKey` values, causing them to survive pool eviction longer than their true fee rate warrants and allowing legitimate high-fee transactions from other users to be evicted in their place.

## Finding Description

**Root cause — link graph cleared before ancestor update:**

`remove_entry_and_descendants` first calls `remove_entry_links` for every entry in the batch (root + all descendants), then calls `remove_entry` on each:

```
// pool_map.rs L252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links stripped here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← remove_entry called after
        .collect()
}
```

`remove_entry_links` removes the entry from its parents' children sets, removes its parents from its own parents set, and deletes its node from the link map entirely:

```
// pool_map.rs L418-430
fn remove_entry_links(&mut self, id: &ProposalShortId) {
    if let Some(parents) = self.links.get_parents(id).cloned() {
        for parent in parents {
            self.links.remove_child(&parent, id);
        }
    }
    ...
    self.links.remove(id);
}
```

Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`:

```
// pool_map.rs L242
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` resolves ancestors by traversing `self.links`:

```
// pool_map.rs L432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),
                ...
            };
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

`calc_ancestors` in `links.rs` starts from the entry's own node in the link map and walks parents recursively. Because `remove_entry_links` already deleted the root's node and severed its connection to surviving ancestors, `calc_ancestors` returns an empty set. **No surviving ancestor ever has `sub_descendant_weight` called on it.**

**Eviction key derivation uses the stale fields:**

```
// entry.rs L234-247
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
        }
    }
}
```

`EvictKey` ordering is ascending by `fee_rate` — the entry with the lowest key is evicted first. An ancestor with inflated `descendants_fee` has an artificially high `fee_rate` in its `EvictKey` and is therefore evicted later than its true fee rate warrants.

**Exploit flow (RBF path):**

1. Attacker submits tx **A** (fee = 100 shannons). A's `descendants_fee` = 100.
2. Attacker submits tx **B** (child of A, fee = 5 000 000 shannons) and tx **C** (child of B, fee = 5 000 000 shannons). A's `descendants_fee` = 10 000 100.
3. Attacker submits tx **B′** conflicting with B (RBF). `process_rbf` calls `remove_entry_and_descendants(B)`, removing B and C. Due to the bug, A's `descendants_fee` remains 10 000 100.
4. B′ is inserted as a new child of A. `record_entry_descendants` → `update_ancestors_index_key(B′, Add)` adds B′'s fee on top of the stale value. A's `descendants_fee` = 10 000 100 + B′_fee (should be 100 + B′_fee).
5. When the pool fills and `limit_size` runs, `next_evict_entry` picks the entry with the lowest `EvictKey`. A's inflated `EvictKey.fee_rate` causes it to survive while legitimate medium/high-fee transactions from other users are evicted.

The same staleness occurs in `resolve_conflict`, `resolve_conflict_header_dep`, and the ancestor-count eviction path inside `check_and_record_ancestors` — any call to `remove_entry_and_descendants` where the removed root has surviving ancestors.

**Existing guards are insufficient:** The comment in the code acknowledges the pre-removal of links is intentional to avoid double-updating descendants, but it silently skips the necessary update to surviving ancestors. There is no compensating mechanism.

## Impact Explanation

The inflated `EvictKey` on surviving ancestors causes the pool's eviction ordering to be incorrect. When the pool is at capacity, `limit_size` evicts legitimate high-fee transactions from other users in preference to the attacker's low-fee ancestor, because the attacker's entry appears more valuable than it is. Each RBF cycle compounds the inflation. This constitutes a low-cost mechanism by which an unprivileged submitter can occupy pool space with a low-fee transaction indefinitely, displacing legitimate transactions and degrading effective pool throughput — matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

- RBF is enabled by default (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`).
- Any unprivileged tx-pool submitter can craft the required transaction chain.
- The tracking corruption occurs unconditionally on every `remove_entry_and_descendants` call where the removed root has surviving ancestors — a routine occurrence during RBF and conflict resolution.
- The eviction impact materializes whenever the pool approaches its size limit, which is a normal operating condition on a busy network.
- No special privileges, keys, or majority hash power are required.

## Recommendation

Before stripping links, capture the surviving ancestors of the root entry, then explicitly decrement their descendant weights for each removed entry after removal:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Capture surviving ancestors BEFORE links are removed
    let surviving_ancestors = self.links.calc_ancestors(id);

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Decrement descendant weights on surviving ancestors for each removed entry
    for removed_entry in &removed {
        for anc_id in &surviving_ancestors {
            self.entries.modify_by_id(anc_id, |e| {
                e.inner.sub_descendant_weight(removed_entry);
                e.evict_key = e.inner.as_evict_key();
            });
        }
    }

    removed
}
```

Alternatively, restructure `remove_entry` to accept an explicit ancestor set so the link graph does not need to be intact at call time.

## Proof of Concept

**Manual steps (pool with RBF enabled, `min_rbf_rate > min_fee_rate`):**

1. Submit tx **A** (fee = 100 shannons, size = S). Verify via `get_pool_tx_detail_info` that A's `descendants_fee` = 100.
2. Submit tx **B** spending A's output (fee = 5 000 000 shannons). Verify A's `descendants_fee` = 5 000 100.
3. Submit tx **C** spending B's output (fee = 5 000 000 shannons). Verify A's `descendants_fee` = 10 000 100.
4. Submit tx **B′** conflicting with B, fee > B's fee + RBF surcharge. Verify B and C are removed. Verify A's `descendants_fee` is still 10 000 100 (bug: should be 100).
5. Submit B′ as child of A (it spends A's output). Verify A's `descendants_fee` = 10 000 100 + B′_fee (should be 100 + B′_fee).
6. Fill the pool with many medium-fee transactions until `total_tx_size > max_tx_pool_size`.
7. Observe via `tx_pool_info` that A survives eviction while medium-fee transactions from other users are dropped, despite A's true fee being only 100 shannons.

**Unit/invariant test:** After any `remove_entry_and_descendants` call, assert that for every surviving entry E, `E.descendants_fee == sum(fee of E's actual descendants in pool) + E.fee`. This invariant is violated by the current implementation whenever the removed root has surviving ancestors.