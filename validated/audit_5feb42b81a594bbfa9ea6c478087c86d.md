Audit Report

## Title
Stale Descendant Metrics on Surviving Ancestors After `remove_entry_and_descendants` Due to Premature Link Removal - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` pre-removes all link entries for the entire batch before calling `remove_entry` on each. Because `remove_entry` relies on `self.links.calc_ancestors` to find which remaining pool entries need their descendant-weight metrics decremented, and those links are already gone, ancestors of the removed chain that remain in the pool are never updated. Their `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and derived `evict_key` fields remain permanently inflated for the lifetime of those ancestors in the pool.

## Finding Description
`remove_entry_and_descendants` (L252–265) first collects the root and all its descendants, then calls `remove_entry_links` for every entry in the batch before calling `remove_entry`: [1](#0-0) 

`remove_entry_links` (L418–430) calls `self.links.remove(id)`, which deletes the entry from `links.inner` entirely: [2](#0-1) 

When `remove_entry` subsequently calls `update_ancestors_index_key` (L242), that function calls `self.links.calc_ancestors(&child.proposal_short_id())` (L433–434): [3](#0-2) 

`calc_ancestors` delegates to `calc_relative_ids` (links.rs L37–50), which does `self.inner.get(short_id)` — returning `None` because the entry was already removed — and falls back to an empty set via `.unwrap_or_default()`: [4](#0-3) 

Because `direct` is empty, `calc_ancestors` returns an empty set, and the loop in `update_ancestors_index_key` that calls `sub_descendant_weight` and recomputes `evict_key` never executes. The comment at L256 acknowledges the intentional link pre-removal ("so that we won't update_descendants_index_key in remove_entry"), but this also silently suppresses the ancestor update for entries that are **not** being removed.

**Concrete scenario (A → B → C, remove B and descendants):**
- `remove_entry_links(B)`: removes B from A's children, removes B from C's parents, removes B from `links.inner`
- `remove_entry_links(C)`: removes C from `links.inner`
- `remove_entry(B)`: `calc_ancestors(B)` → `inner.get(B)` → `None` → empty → A's `descendants_*` fields and `evict_key` are never decremented
- A retains inflated `descendants_count`, `descendants_fee`, etc. for its entire remaining lifetime in the pool

## Impact Explanation
The corrupted `evict_key` on surviving ancestors directly affects pool eviction ordering. `next_evict_entry` (L380–385) selects entries via `iter_by_evict_key()`: [5](#0-4) 

`EvictKey` (sort_key.rs L80–103) compares first by `fee_rate`, then by `descendants_count`, then by `timestamp`: [6](#0-5) 

An ancestor with an inflated `descendants_count` is ranked differently in the eviction order than it should be, causing incorrect eviction decisions when the pool exceeds `max_tx_pool_size`. This is a concrete, persistent accounting inconsistency affecting which transactions are retained or dropped. This matches **Low (501–2000 points) — any other important performance improvements for CKB**, as the eviction ordering corruption degrades pool fairness and efficiency.

## Likelihood Explanation
The bug is triggered whenever `remove_entry_and_descendants` is called on an entry that has at least one ancestor still in the pool. This is reachable by any unprivileged user via:
- **RBF**: submitting a higher-fee conflicting transaction triggers `process_rbf` → `remove_entry_and_descendants`
- **Block commitment**: `resolve_conflict` (L305–331) calls `remove_entry_and_descendants` for any pool transaction spending a committed input
- **Pool size enforcement**: `limit_size` (pool.rs L292–329) calls `remove_entry_and_descendants` on the eviction candidate
- **`remove_tx` RPC**: pool.rs L358–361 [7](#0-6) 

The RBF path requires no privilege and is repeatable. The stale state persists for the full lifetime of the surviving ancestor in the pool, with no recomputation mechanism.

## Recommendation
Before the loop that calls `remove_entry_links` for all entries, call `update_ancestors_index_key` for each entry being removed (in topological order from leaves to root) so that ancestors remaining in the pool have their `descendants_*` fields and `evict_key` correctly decremented before link teardown. Alternatively, restructure `remove_entry` to accept a flag that skips link-based ancestor lookup, and perform the ancestor update explicitly before link removal in `remove_entry_and_descendants`.

## Proof of Concept
1. Submit Tx A to the pool. A's `descendants_count = 1`, `descendants_fee = fee_A`.
2. Submit Tx B (child of A). A's `descendants_count = 2`, `descendants_fee = fee_A + fee_B`.
3. Submit Tx C (child of B). A's `descendants_count = 3`, `descendants_fee = fee_A + fee_B + fee_C`.
4. Submit Tx D spending the same input as B with a higher fee (RBF). `process_rbf` calls `remove_entry_and_descendants(B)`, removing B and C.
5. After removal: inspect A's `descendants_count` — it is still `3`; `descendants_fee` still includes `fee_B + fee_C`; A's `evict_key` is inflated.
6. Fill the pool to `max_tx_pool_size`. `limit_size` calls `next_evict_entry`, which iterates by `evict_key`. A's inflated `descendants_count` in its `evict_key` causes it to be ranked incorrectly, so a lower-fee-rate transaction that should survive is evicted instead.
7. A unit test can assert `pool_map.get(A_id).descendants_count == 1` after step 4; it will fail, confirming the bug.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
    }
```

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

**File:** tx-pool/src/component/pool_map.rs (L418-430)
```rust
    fn remove_entry_links(&mut self, id: &ProposalShortId) {
        if let Some(parents) = self.links.get_parents(id).cloned() {
            for parent in parents {
                self.links.remove_child(&parent, id);
            }
        }
        if let Some(children) = self.links.get_children(id).cloned() {
            for child in children {
                self.links.remove_parent(&child, id);
            }
        }
        self.links.remove(id);
    }
```

**File:** tx-pool/src/component/pool_map.rs (L432-445)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
        for anc_id in &ancestors {
            // update parent score
            self.entries.modify_by_id(anc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_descendant_weight(child),
                    EntryOp::Add => e.inner.add_descendant_weight(child),
                };
                e.evict_key = e.inner.as_evict_key();
            });
        }
    }
```

**File:** tx-pool/src/component/links.rs (L37-50)
```rust
    fn calc_relative_ids(
        &self,
        short_id: &ProposalShortId,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let direct = self
            .inner
            .get(short_id)
            .map(|link| link.get_direct_ids(relation))
            .cloned()
            .unwrap_or_default();

        self.calc_relation_ids(direct, relation)
    }
```

**File:** tx-pool/src/component/sort_key.rs (L80-103)
```rust
pub struct EvictKey {
    pub fee_rate: FeeRate,
    pub timestamp: u64,
    pub descendants_count: usize,
}

impl PartialOrd for EvictKey {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
```

**File:** tx-pool/src/pool.rs (L306-308)
```rust
            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
```
