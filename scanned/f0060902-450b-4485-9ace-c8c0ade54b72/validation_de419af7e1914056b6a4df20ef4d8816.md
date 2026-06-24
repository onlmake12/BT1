The code is confirmed. Let me trace the exact execution path to validate the claim.

**Phase 1** — `remove_entry_and_descendants` calls `remove_entry_links(id)` for every entry in `removed_ids` (root + all descendants). [1](#0-0) 

`remove_entry_links` calls `self.links.remove(id)` at the end, which removes the entry from `self.links.inner`. [2](#0-1) 

**Phase 2** — `remove_entry` is then called for each entry, which calls `update_ancestors_index_key`. [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`. [4](#0-3) 

`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)` returns `None` (because `remove_entry_links` already called `self.links.remove(id)`), so `direct` is an empty set via `.unwrap_or_default()`, and the ancestor loop never executes. [5](#0-4) 

The existing `test_remove_entry_and_descendants` test only checks that tx2/tx3 are absent and tx1's descendant set is empty — it never asserts that tx1's `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, or `evict_key` are decremented. [6](#0-5) 

The `EvictKey` is computed from `descendants_fee` and `descendants_size/cycles`, so a stale `descendants_fee` directly inflates the `evict_key`. [7](#0-6) 

`next_evict_entry` iterates in ascending `evict_key` order, so an inflated key causes the ancestor to be skipped during eviction. [8](#0-7) 

`limit_size` relies solely on `next_evict_entry` to select which transaction to drop. [9](#0-8) 

The bug is real and the impact is confirmed. The severity fits Medium ("Suboptimal implementation of CKB state storage mechanism") — the tx pool is a core node component and the eviction ordering is permanently corrupted for surviving ancestors until they are removed and re-added.

---

Audit Report

## Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` strips all parent/child links for the entire removed subtree before calling `remove_entry` on each entry. Because `update_ancestors_index_key` resolves ancestors through those same links, the call inside `remove_entry` finds an empty ancestor set for the root entry and silently skips updating the `descendants_*` fields and `evict_key` of surviving pool ancestors. Those ancestors permanently carry stale, inflated eviction keys, corrupting the pool's eviction ordering until the node restarts or those ancestors are themselves removed.

## Finding Description
`remove_entry_and_descendants` operates in two phases:

**Phase 1** collects the root and all its descendants, then calls `remove_entry_links` on every entry in that set:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // removes ALL parent/child links
}
```

`remove_entry_links` ends with `self.links.remove(id)`, which deletes the entry from `self.links.inner`.

**Phase 2** calls `remove_entry` for each entry. `remove_entry` immediately calls:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`, which internally calls `calc_relative_ids`. That function does:

```rust
let direct = self.inner.get(short_id)
    .map(|link| link.get_direct_ids(relation))
    .cloned()
    .unwrap_or_default();   // ← returns empty HashSet
```

Because Phase 1 already called `self.links.remove(root_id)`, `self.inner.get(root_id)` returns `None`, `direct` is empty, and `calc_ancestors` returns an empty set. The loop body in `update_ancestors_index_key` never executes. The ancestors of the root entry — which remain in the pool — never have `sub_descendant_weight` called on them, so their `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and `evict_key` are never decremented.

The comment in Phase 1 acknowledges the intent is to skip `update_descendants_index_key` (correct — descendants are all being removed). But it silently also skips `update_ancestors_index_key`, which is incorrect because the ancestors remain in the pool.

The existing `test_remove_entry_and_descendants` test only verifies that the removed entries are absent and that the descendant set of the root's parent is empty. It never asserts that the parent's `descendants_fee` or `evict_key` are updated, leaving the regression undetected.

## Impact Explanation
`EvictKey` is computed from `descendants_fee` and `descendants_size/cycles`. A stale `descendants_fee` on an ancestor makes its `evict_key` appear higher (more valuable) than it actually is. `next_evict_entry` iterates entries in ascending `evict_key` order and picks the first match. `limit_size` uses `next_evict_entry` as the sole mechanism to select which transaction to drop when the pool exceeds `max_tx_pool_size`. A stale, inflated `evict_key` on an ancestor means `limit_size` skips it and instead evicts a genuinely higher-fee transaction. The result is a low-fee ancestor transaction surviving pool pressure it should not survive, while legitimate high-fee transactions submitted by other users are rejected with `Reject::Full`. This constitutes a suboptimal implementation of the CKB tx-pool state management mechanism (Medium, 2001–10000 points).

## Likelihood Explanation
Any unprivileged `send_transaction` RPC caller can reach this path. The attacker submits a parent tx `P` with a low fee rate, then submits child txs `C1…Cn` with high fee rates (boosting `P`'s `descendants_fee` and `evict_key`). The attacker then triggers removal of `C1…Cn` via RBF (submitting a conflicting tx that spends the same inputs), which is straightforward when `min_rbf_rate > min_fee_rate` (a common node configuration). After removal, `P`'s `evict_key` is stale/inflated. The attacker then fills the pool with many small transactions to trigger `limit_size`. `P` is not evicted; other users' high-fee transactions are rejected instead. `remove_entry_and_descendants` is also reachable from `resolve_conflict_header_dep`, `check_and_record_ancestors`, `remove_by_detached_proposal`, and `remove_tx`, providing additional trigger paths without requiring RBF.

## Recommendation
Before stripping links in `remove_entry_and_descendants`, explicitly update the ancestors of the root entry while the links are still intact:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors of the root entry BEFORE links are torn down
    if let Some(root_entry) = self.entries.get_by_id(id) {
        let inner = root_entry.inner.clone();
        self.update_ancestors_index_key(&inner, EntryOp::Remove);
    }

    // strip links so that remove_entry won't re-run update_descendants_index_key
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

This mirrors the pattern used in the single-entry `remove_entry` path, which correctly calls both `update_ancestors_index_key` and `update_descendants_index_key` before removing links.

## Proof of Concept
Using the existing test infrastructure in `tx-pool/src/component/tests/score_key.rs`:

1. Build a chain `tx1 → tx2 → tx3` (tx2 spends tx1's output, tx3 spends tx2's output), all with `fee=100, size=100`.
2. Insert all three into the pool via `add_proposed`.
3. After insertion, `tx1.descendants_fee` should equal `300` (self + tx2 + tx3), and `tx1.evict_key` reflects a high descendants fee rate.
4. Call `pool_map.remove_entry_and_descendants(&tx2_id)` (removes tx2 and tx3).
5. **Expected:** `tx1.descendants_fee == 100` (only itself), `tx1.evict_key` recomputed to reflect its actual lone fee rate.
6. **Actual:** `tx1.descendants_fee` remains `300`, `tx1.evict_key` is unchanged.
7. Assert `pool_map.get(&tx1_id).unwrap().descendants_fee == Capacity::shannons(100)` — this assertion fails, proving the bug.

The existing `test_remove_entry_and_descendants` test at line 171 of `score_key.rs` can be extended with this assertion to serve as a regression test.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L235-250)
```rust
    pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
        self.entries.remove_by_id(id).map(|entry| {
            debug!(
                "remove entry {} from status: {:?}",
                entry.inner.transaction().hash(),
                entry.status
            );
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
            self.remove_entry_links(id);
            self.track_entry_statics(Some(entry.status), None);
            self.update_stat_for_remove_tx(entry.inner.size, entry.inner.cycles);
            entry.inner
        })
    }
```

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

**File:** tx-pool/src/component/tests/score_key.rs (L171-230)
```rust
fn test_remove_entry_and_descendants() {
    let mut map = PoolMap::new(DEFAULT_MAX_ANCESTORS_COUNT);
    let tx1 = TxEntry::dummy_resolve(
        TransactionBuilder::default().build(),
        100,
        Capacity::shannons(100),
        100,
    );
    let tx2 = TxEntry::dummy_resolve(
        TransactionBuilder::default()
            .input(
                CellInput::new_builder()
                    .previous_output(
                        OutPoint::new_builder()
                            .tx_hash(tx1.transaction().hash())
                            .index(0u32)
                            .build(),
                    )
                    .build(),
            )
            .witness(Bytes::new())
            .build(),
        200,
        Capacity::shannons(200),
        200,
    );
    let tx3 = TxEntry::dummy_resolve(
        TransactionBuilder::default()
            .input(
                CellInput::new_builder()
                    .previous_output(
                        OutPoint::new_builder()
                            .tx_hash(tx2.transaction().hash())
                            .index(0u32)
                            .build(),
                    )
                    .build(),
            )
            .witness(Bytes::new())
            .build(),
        200,
        Capacity::shannons(200),
        200,
    );
    let tx1_id = tx1.proposal_short_id();
    let tx2_id = tx2.proposal_short_id();
    let tx3_id = tx3.proposal_short_id();
    map.add_proposed(tx1).unwrap();
    map.add_proposed(tx2).unwrap();
    map.add_proposed(tx3).unwrap();
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(descendants_set.contains(&tx2_id));
    assert!(descendants_set.contains(&tx3_id));
    map.remove_entry_and_descendants(&tx2_id);
    assert!(!map.contains_key(&tx2_id));
    assert!(!map.contains_key(&tx3_id));
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(!descendants_set.contains(&tx2_id));
    assert!(!descendants_set.contains(&tx3_id));
}
```

**File:** tx-pool/src/component/entry.rs (L234-247)
```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let weight = get_transaction_weight(entry.size, entry.cycles);
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
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```
