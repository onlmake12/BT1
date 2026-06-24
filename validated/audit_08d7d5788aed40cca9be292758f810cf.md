Audit Report

## Title
Stale Descendant-Weight Fields on Surviving Pool Ancestors After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` pre-strips all `TxLinksMap` entries for the target and its entire descendant set before calling `remove_entry` on each node. Because `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`, and those links are already gone by the time `remove_entry` runs, any pool transaction that is an ancestor of the removed subtree root but is not itself in the removed set never receives `sub_descendant_weight` calls. Its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are permanently inflated, corrupting `EvictKey` ordering for all subsequent eviction decisions.

## Finding Description

**Root cause — `remove_entry_and_descendants` (lines 252–265):**

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips ALL links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry_links` (lines 418–430) removes the node's entry from `TxLinksMap.inner` entirely and also removes cross-references from its parents and children. After the pre-strip loop, every node in `removed_ids` is gone from `self.links`.

**Why surviving ancestors are never updated:**

`remove_entry` (lines 235–250) calls `update_ancestors_index_key` (lines 432–445):

```rust
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

`calc_ancestors` (links.rs lines 37–50) walks `TxLinksMap.inner` starting from the node's own entry. Because the pre-strip phase already called `self.links.remove(id)` for every node in `removed_ids`, `calc_ancestors` returns an empty set for each of them. `sub_descendant_weight` is never called on any surviving ancestor.

**Concrete scenario — chain X → A → B → C:**

Pool contains X (surviving ancestor) → A → B → C. A block commits a transaction that double-spends A's input. `resolve_conflict` calls `remove_entry_and_descendants(&A_id)` (lines 305–316). `removed_ids = [A, B, C]`. All three links are stripped. When `remove_entry(A/B/C)` runs, `calc_ancestors` returns `{}` for each. X's `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles` are never decremented and remain inflated by the combined weight of A, B, and C.

**`EvictKey` construction (entry.rs lines 234–247):**

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,
        }
    }
}
```

All three fields (`fee_rate` via stale `descendants_fee`/`descendants_size`/`descendants_cycles`, and `descendants_count`) are derived from the stale fields. X's stored `evict_key` is never refreshed after the removal because `update_ancestors_index_key` found no ancestors.

**Note on fee-rate estimate claim:** The submitted report's claim that `estimate_fee_rate` is affected is incorrect. `pool_map::estimate_fee_rate` iterates by `score` (`AncestorsScoreSortKey`), which is based on ancestor weights, not descendant weights. The `WeightUnitsFlow` estimator similarly uses only `info.size` and `info.cycles` per transaction. The stale descendant fields do not corrupt fee-rate estimates.

## Impact Explanation

`next_evict_entry` (lines 380–385) iterates by `evict_key`:

```rust
pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
    self.entries
        .iter_by_evict_key()
        .find(move |entry| entry.status == status)
        .map(|entry| entry.id.clone())
}
```

An inflated `descendants_feerate` in X's stale `evict_key` makes X appear more valuable than it actually is, so it is placed later in the eviction order than it should be. X survives eviction rounds it should lose; legitimate higher-value transactions may be evicted in its place. This is a persistent corruption: the stale fields are never corrected after the removal, so every subsequent eviction decision involving X is wrong until X itself is eventually removed.

**Impact class: Low (501–2000 points) — important performance/correctness improvement for CKB tx-pool eviction.**

## Likelihood Explanation

The trigger requires two steps: (1) submit a chain X → A → B → C via standard P2P/RPC (fully unprivileged), and (2) have a transaction conflicting with A included in a block. Step 2 requires either mining power or a miner that accepts out-of-pool transactions. The bug also fires naturally whenever any block contains a transaction that conflicts with a pool transaction that itself has pool ancestors — a routine occurrence on mainnet during normal operation. The bug is therefore reachable without deliberate attack, and repeatable across every such block event.

## Recommendation

In `remove_entry_and_descendants`, update surviving ancestors' descendant-weight fields **before** stripping links. The minimal fix: for each node being removed, call `update_ancestors_index_key(node, EntryOp::Remove)` while links are still intact, then strip the links. Alternatively, collect the set of surviving ancestors (those not in `removed_ids`) before any stripping and explicitly call `sub_descendant_weight` on them for each removed node, then update their `evict_key`.

The existing comment `// update links state for remove, so that we won't update_descendants_index_key in remove_entry` correctly explains the intent (avoid updating already-removed descendants' `score`), but the implementation also inadvertently suppresses the necessary ancestor `evict_key` updates.

## Proof of Concept

1. Build a `PoolMap` with chain **X → A → B → C** (each spending the previous tx's output). Add all four as `Pending` entries.
2. Record `X.descendants_count` (expected: 3) and `X.descendants_fee` (expected: sum of A+B+C fees).
3. Call `pool_map.remove_entry_and_descendants(&A_id)`.
4. Assert `pool_map.contains_key(&X_id)` is `true` (X survives).
5. Retrieve X's `PoolEntry` and assert `entry.inner.descendants_count == 0` — **this assertion fails**; the field still reads 3.
6. Assert `entry.inner.descendants_fee == X.fee` (only X's own fee) — **this assertion fails**; the field retains the inflated sum.
7. Assert `entry.evict_key == X.inner.as_evict_key()` after recomputing from correct fields — **this assertion fails**; the stored `evict_key` reflects the stale descendant weights.
8. Call `pool_map.next_evict_entry(Status::Pending)` and observe that X is ordered incorrectly relative to other pool entries with accurate `evict_key` values.