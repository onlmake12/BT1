Audit Report

## Title
Stale Descendant Metrics on Surviving Ancestors After `remove_entry_and_descendants` Due to Premature Link Removal - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` removes all link entries for the entire batch (root + descendants) before calling `remove_entry` on each. Because `remove_entry` relies on `self.links.calc_ancestors` to find which remaining pool entries need their descendant-weight metrics decremented, and those links are already gone, ancestors of the removed chain that **remain in the pool** are never updated. Their `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and derived `evict_key` fields remain permanently inflated for the lifetime of those ancestors in the pool.

## Finding Description
`remove_entry_and_descendants` (L252–265) first collects the root and all its descendants, then calls `remove_entry_links` for every entry in the batch before calling `remove_entry`:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // removes id from links.inner entirely
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) calls `self.links.remove(id)`, which deletes the entry from `links.inner`. When `remove_entry` subsequently calls `update_ancestors_index_key` (L242), that function calls `self.links.calc_ancestors(&child.proposal_short_id())` (L433–434), which calls `calc_relative_ids` (links.rs L37–50):

```rust
let direct = self.inner.get(short_id)   // returns None — already removed
    .map(|link| link.get_direct_ids(relation))
    .cloned()
    .unwrap_or_default();              // empty set
```

Because `direct` is empty, `calc_ancestors` returns an empty set, and the loop in `update_ancestors_index_key` that calls `sub_descendant_weight` and recomputes `evict_key` never executes. The code comment at L256 acknowledges the intentional link pre-removal ("so that we won't update_descendants_index_key in remove_entry"), but this also silently suppresses the ancestor update for entries that are **not** being removed.

**Concrete scenario (A → B → C, remove B and descendants):**
- `remove_entry_links(B)`: removes B from A's children, removes B from C's parents, removes B from `links.inner`
- `remove_entry_links(C)`: removes C from `links.inner`
- `remove_entry(B)`: `calc_ancestors(B)` → `inner.get(B)` → `None` → empty → A's `descendants_*` fields and `evict_key` are never decremented
- A retains inflated `descendants_count`, `descendants_fee`, etc. for its entire remaining lifetime in the pool

## Impact Explanation
The corrupted `evict_key` on surviving ancestors directly affects pool eviction ordering. `next_evict_entry` (L380–385) selects entries via `iter_by_evict_key()`. Ancestors with inflated descendant fee-rate appear more valuable than they are and are skipped during eviction. As a result, lower-fee-rate transactions that should survive are evicted instead of the stale-metric ancestors. This constitutes incorrect pool management behavior — a concrete, persistent accounting inconsistency affecting which transactions are retained or dropped when the pool exceeds `max_tx_pool_size`. This matches the allowed impact: **Low (501–2000 points) — any other important performance improvements for CKB**, as the eviction ordering corruption degrades pool fairness and efficiency. The `estimate_fee_rate` path uses `iter_by_score()` (ancestor-based `AncestorsScoreSortKey`), not `evict_key`, so that secondary impact claimed in the report is not directly caused by this bug.

## Likelihood Explanation
The bug is triggered whenever `remove_entry_and_descendants` is called on an entry that has at least one ancestor still in the pool. This is reachable by any unprivileged user via:
- **RBF**: submitting a higher-fee conflicting transaction triggers `process_rbf` → `remove_entry_and_descendants` (process.rs L203–206)
- **Block commitment**: `resolve_conflict` (L305–331) calls `remove_entry_and_descendants` for any pool transaction spending a committed input
- **Pool size enforcement**: `limit_size` (pool.rs L306–308) calls `remove_entry_and_descendants` on the eviction candidate
- **`remove_tx` RPC**: pool.rs L358–361

The RBF path requires no privilege and is repeatable. The stale state persists for the full lifetime of the surviving ancestor in the pool, with no recomputation mechanism.

## Recommendation
Before the loop that calls `remove_entry_links` for all entries, call `update_ancestors_index_key` for the root entry (and each descendant in topological order from leaves to root) so that ancestors remaining in the pool have their `descendants_*` fields and `evict_key` correctly decremented before link teardown. Alternatively, restructure `remove_entry` to accept a flag that skips link-based ancestor lookup, and perform the ancestor update explicitly before link removal in `remove_entry_and_descendants`.

## Proof of Concept
1. Submit Tx A to the pool. A's `descendants_count = 1`, `descendants_fee = fee_A`.
2. Submit Tx B (child of A). A's `descendants_count = 2`, `descendants_fee = fee_A + fee_B`.
3. Submit Tx C (child of B). A's `descendants_count = 3`, `descendants_fee = fee_A + fee_B + fee_C`.
4. Submit Tx D spending the same input as B with a higher fee (RBF). `process_rbf` calls `remove_entry_and_descendants(B)`, removing B and C.
5. After removal: inspect A's `descendants_count` — it is still `3`; `descendants_fee` still includes `fee_B + fee_C`; A's `evict_key` is inflated.
6. Fill the pool to `max_tx_pool_size`. `limit_size` calls `next_evict_entry`, which iterates by `evict_key`. A's inflated `evict_key` causes it to be ranked as more valuable than it actually is, so it is skipped. A lower-fee-rate transaction that should survive is evicted instead.
7. A unit test can assert `pool_map.get(A_id).descendants_count == 1` after step 4; it will fail, confirming the bug.