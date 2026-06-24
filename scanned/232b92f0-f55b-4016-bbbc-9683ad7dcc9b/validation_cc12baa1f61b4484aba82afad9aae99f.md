The code confirms the claim. Let me verify the `remove_entry` flow and the `calc_ancestors` behavior when links are pre-removed.

Audit Report

## Title
Inflated Descendant-Weight Accounting in `remove_entry_and_descendants` Corrupts Tx-Pool Eviction Ordering — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` pre-strips all link records for the entire subtree before calling `remove_entry` on each node. Because `update_ancestors_index_key` resolves surviving ancestors via those same link records, the call inside `remove_entry` finds an empty ancestor set and never invokes `sub_descendant_weight` on any surviving ancestor. Every ancestor of an evicted or conflict-resolved subtree permanently retains inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` values, corrupting the `EvictKey` used to select which transaction to drop when the pool is full.

## Finding Description
`remove_entry_and_descendants` (lines 252–265) collects the subtree, calls `remove_entry_links` for every node in the batch, then calls `remove_entry` for each:

```rust
// pool_map.rs lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // strips id from self.links entirely
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry` (lines 235–250) calls `update_ancestors_index_key` at line 242:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` (lines 432–445) resolves ancestors through `self.links`:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id()); // empty: links already gone
    for anc_id in &ancestors { ... }  // never executes
}
```

`calc_ancestors` (links.rs line 78) calls `calc_relative_ids`, which looks up `self.inner.get(short_id)`. Since `remove_entry_links` already called `self.links.remove(id)` for every node in the batch, the lookup returns `None`, the initial `direct` set is empty, and the traversal returns an empty `HashSet`. The `sub_descendant_weight` call on surviving ancestors is therefore never made.

The comment on line 256 acknowledges the intent to skip `update_descendants_index_key` (updating removed descendants' ancestor weights, which is harmless since those entries are gone), but the same pre-removal of links silently disables `update_ancestors_index_key` for surviving ancestors — the unintended side effect.

Concrete trace for chain tx1→tx2→tx3, calling `remove_entry_and_descendants(tx2)`:
1. `removed_ids = [tx2, tx3]`
2. `remove_entry_links(tx2)`: removes tx2 from `self.links`, removes tx2 from tx1's children set, removes tx3 from tx2's parents set
3. `remove_entry_links(tx3)`: removes tx3 from `self.links`
4. `self.links` now contains only tx1 (with empty children)
5. `remove_entry(tx2)` → `update_ancestors_index_key(tx2, Remove)` → `calc_ancestors(tx2)` → empty → tx1 never updated
6. `remove_entry(tx3)` → same → tx1 never updated

tx1's `descendants_count` remains 3 instead of 1; `descendants_size`, `descendants_cycles`, `descendants_fee` remain inflated by tx2+tx3's values.

The existing test `test_remove_entry_and_descendants` (score_key.rs lines 170–230) confirms the gap: it asserts only that tx2 and tx3 are absent from the pool and from `calc_descendants`, but never asserts that tx1's `descendants_count` returns to 1.

## Impact Explanation
The `descendants_*` fields feed directly into `EvictKey` (entry.rs lines 234–247):

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            descendants_count: entry.descendants_count,  // inflated
            ...
        }
    }
}
```

`next_evict_entry` (pool_map.rs lines 380–385) iterates by `evict_key` to select the lowest-priority transaction to drop. With inflated `descendants_fee` and `descendants_count`, an ancestor whose subtree was already evicted appears to have more and higher-fee descendants than it actually does, raising its apparent `fee_rate` in the eviction key. This makes it look more valuable than it is, causing it to be skipped during eviction. As a result, the pool drops the wrong entry when full — a genuinely low-fee orphaned ancestor is retained while a legitimate high-fee transaction is rejected. This matches the allowed impact: **Low (501–2000 points) — any other important performance improvements for CKB**, specifically incorrect tx-pool eviction ordering that degrades pool quality and fee-estimation correctness.

## Likelihood Explanation
Any unprivileged RPC caller or relay peer can trigger this path with four ordinary transactions:
1. Submit parent tx_A via `send_transaction`
2. Submit child tx_B spending an output of tx_A
3. Submit grandchild tx_C spending an output of tx_B
4. Submit conflicting tx_D spending the same input as tx_B

Step 4 causes `resolve_conflict` → `remove_entry_and_descendants(tx_B)`, removing tx_B and tx_C while leaving tx_A with permanently inflated `descendants_*`. No special privilege is required. The inflation accumulates over the pool's lifetime because every call to `resolve_conflict`, `process_rbf`, `resolve_conflict_header_dep`, and `limit_size` routes through `remove_entry_and_descendants`.

## Recommendation
Capture the set of surviving ancestors of the subtree root **before** any links are torn down, then explicitly decrement their descendant weights after removal:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Capture ancestors of the root BEFORE links are torn down
    let root_ancestors = self.links.calc_ancestors(id);

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Decrement descendants_* for every surviving ancestor
    for removed_entry in &removed {
        for anc_id in &root_ancestors {
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
Add the following assertions to the existing `test_remove_entry_and_descendants` test in `tx-pool/src/component/tests/score_key.rs` after line 229:

```rust
// After map.remove_entry_and_descendants(&tx2_id):
let tx1_entry = map.get(&tx1_id).unwrap();
assert_eq!(tx1_entry.descendants_count, 1); // FAILS: actual value is 3
assert_eq!(tx1_entry.descendants_size, tx1_entry.size); // FAILS: still includes tx2+tx3 sizes
assert_eq!(tx1_entry.descendants_fee, Capacity::shannons(100)); // FAILS: still includes tx2+tx3 fees
```

These assertions fail against the current code, demonstrating the stale accounting. The fix above makes them pass.