Audit Report

## Title
Stale Descendant Weight After `remove_entry_and_descendants` Causes Incorrect Pool Eviction Ordering — (`File: tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` strips all link-graph entries for the entire removal batch before invoking `remove_entry` on each one. Because `update_ancestors_index_key` resolves ancestors through `TxLinksMap::calc_ancestors`, which requires the removed entry's link record to be present, the link pre-removal causes `calc_ancestors` to return an empty set. Surviving ancestors of the removed subtree therefore never have their `descendants_fee`, `descendants_size`, `descendants_cycles`, or `descendants_count` decremented, leaving permanently inflated `EvictKey` values that distort pool eviction ordering.

## Finding Description

**Root cause — link pre-removal before ancestor update:**

`remove_entry_and_descendants` first calls `remove_entry_links` for every entry in the batch:

```rust
// pool_map.rs L252-265
for id in &removed_ids {
    self.remove_entry_links(id);   // strips id from link graph entirely
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` removes the entry from its parents' children sets, from its children's parents sets, and then deletes its own record from `TxLinksMap::inner`:

```rust
// pool_map.rs L418-430
self.links.remove(id);   // entry's own HashMap record gone
```

`remove_entry` then calls `update_ancestors_index_key`:

```rust
// pool_map.rs L242
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` resolves ancestors via `calc_ancestors`:

```rust
// pool_map.rs L432-434
let ancestors: HashSet<ProposalShortId> =
    self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` → `calc_relative_ids` → `calc_relation_ids` starts by reading the entry's own link record:

```rust
// links.rs L42-47
let direct = self
    .inner
    .get(short_id)          // entry already removed → None
    .map(|link| link.get_direct_ids(relation))
    .cloned()
    .unwrap_or_default();   // returns empty HashSet
```

Because the link record was already deleted, `direct` is empty, `calc_relation_ids` returns an empty set, and the loop in `update_ancestors_index_key` never executes. No surviving ancestor ever receives a `sub_descendant_weight` call.

**Concrete RBF scenario (A → B → C, then B replaced by B′):**

1. A inserted: `descendants_fee(A) = fee_A`.
2. B inserted as child of A: `descendants_fee(A) += fee_B`.
3. C inserted as child of B: `descendants_fee(A) += fee_C`.
4. B′ conflicts with B; `process_rbf` calls `remove_entry_and_descendants(B)`.
   - `remove_entry_links(B)` and `remove_entry_links(C)` run first.
   - `remove_entry(B)` → `calc_ancestors(B)` → empty → A not updated.
   - `remove_entry(C)` → `calc_ancestors(C)` → empty → A not updated.
   - A's `descendants_fee` still equals `fee_A + fee_B + fee_C`.
5. B′ inserted as child of A: `record_entry_descendants` calls `update_ancestors_index_key(B′, Add)`.
   - A's `descendants_fee` becomes `fee_A + fee_B + fee_C + fee_B′` (correct: `fee_A + fee_B′`).
6. `limit_size` calls `next_evict_entry` which iterates `iter_by_evict_key()` ascending; A's inflated `EvictKey.fee_rate` places it later in the eviction order than warranted, so legitimate higher-true-fee-rate transactions from other users are evicted first.

The same staleness occurs in `resolve_conflict`, `resolve_conflict_header_dep`, and the ancestor-count eviction path inside `check_and_record_ancestors` — any call site where the removed root has surviving pool ancestors.

**Why existing guards do not prevent this:**

The comment in the code (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) shows the pre-removal is intentional to suppress redundant descendant updates within the batch, but it silently also suppresses the necessary ancestor updates for entries *outside* the batch. There is no compensating step to update surviving ancestors.

## Impact Explanation

The bug permanently inflates `EvictKey.fee_rate` for any pool entry that is an ancestor of a removed subtree. `limit_size` selects the eviction victim by iterating `iter_by_evict_key()` in ascending order; an entry with an overstated fee-rate is skipped in favour of entries with accurate (lower) fee-rates. A low-fee transaction can therefore survive pool eviction while legitimate higher-fee transactions from other users are dropped. This constitutes a suboptimal implementation of the CKB transaction-pool state storage mechanism, matching **Medium (2001–10000 points): Suboptimal implementation of CKB state storage mechanism**.

## Likelihood Explanation

- RBF is enabled by default (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`).
- Any unprivileged submitter can craft the required A → B → C chain and then submit a conflicting B′.
- The tracking corruption occurs unconditionally on every `remove_entry_and_descendants` call where the removed root has surviving ancestors; no special privileges, keys, or hash power are required.
- The eviction impact materialises only when the pool is near capacity, but the metric corruption itself is unconditional and compounds with each RBF cycle.

## Recommendation

Before stripping links, capture the surviving ancestors of the root entry and explicitly decrement their descendant weights for each removed entry:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Capture surviving ancestors BEFORE any links are removed
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

## Proof of Concept

1. Configure a node with RBF enabled (`min_rbf_rate > min_fee_rate`, the default).
2. Submit tx **A** (fee = 100 shannons, small size).
3. Submit tx **B** spending A's output (fee = 5 000 000 shannons).
4. Submit tx **C** spending B's output (fee = 5 000 000 shannons).
   → Verify via `get_pool_tx_detail_info(A)` that `descendants_fee = 10 000 100`.
5. Submit tx **B′** conflicting with B, fee > B's fee + RBF surcharge.
   → `process_rbf` calls `remove_entry_and_descendants(B)`.
6. Verify via `get_pool_tx_detail_info(A)` that `descendants_fee` is still `10 000 100` (bug: should be `100`).
7. Submit **B′** as child of A; verify `descendants_fee(A)` grows further instead of resetting.
8. Fill the pool with medium-fee transactions until `total_tx_size > max_tx_pool_size`.
9. Observe via `tx_pool_info` that A survives eviction while medium-fee transactions are dropped, despite A's true fee being only 100 shannons.

A unit test can assert `pool_entry.inner.descendants_fee == entry_a.fee + entry_b_prime.fee` after the RBF replacement to catch the regression directly.