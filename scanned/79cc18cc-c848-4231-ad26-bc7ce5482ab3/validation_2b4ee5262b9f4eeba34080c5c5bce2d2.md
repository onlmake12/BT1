The code confirms the vulnerability. Let me verify the key facts:

1. `remove_entry_and_descendants` strips all links first (lines 257-259), then calls `remove_entry` per entry
2. `remove_entry` calls `update_ancestors_index_key` (line 242), which calls `self.links.calc_ancestors` (line 434) — but links are already gone, so it returns empty
3. `sub_descendant_weight` is never called on ancestors that remain in the pool
4. The existing `test_remove_entry_and_descendants` test (lines 224-229) only checks that removed entries are gone — it never asserts `tx1`'s `descendants_count/fee/size/cycles`
5. `next_evict_entry` (line 380-385) iterates by `evict_key`, which is computed from the now-stale `descendants_fee/size/cycles/count` fields

The bug is real and confirmed.

---

Audit Report

## Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Score — (`tx-pool/src/component/pool_map.rs`)

## Summary
`PoolMap::remove_entry_and_descendants` tears down all link records for every entry it is about to delete before calling `remove_entry` on each one. Because `update_ancestors_index_key` resolves the ancestor set through those same link records, it always finds an empty set and never calls `sub_descendant_weight` on ancestor transactions that remain in the pool. Those ancestors permanently carry inflated `descendants_fee / descendants_size / descendants_cycles / descendants_count`, corrupting the `EvictKey` used to decide which transactions to drop when the pool is full.

## Finding Description
`remove_entry_and_descendants` first strips every link record, then iterates and calls `remove_entry` on each collected id:

```rust
// tx-pool/src/component/pool_map.rs lines 252-264
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
``` [1](#0-0) 

Inside `remove_entry`, `update_ancestors_index_key` is called at line 242: [2](#0-1) 

`update_ancestors_index_key` resolves the ancestor set via `self.links.calc_ancestors`: [3](#0-2) 

Because `remove_entry_links` was already called for every entry in the pre-pass, `calc_ancestors` returns an empty `HashSet` for every entry being processed. The `for anc_id in &ancestors` loop body never executes, so `sub_descendant_weight` is never called on any ancestor that remains in the pool. [4](#0-3) 

The `sub_descendant_weight` method that should have been called: [5](#0-4) 

The existing test `test_remove_entry_and_descendants` only asserts that the removed entries are absent from the pool and that `calc_descendants` no longer lists them. It never checks `tx1`'s `descendants_count`, `descendants_fee`, `descendants_size`, or `descendants_cycles` after the call, so the bug is not caught: [6](#0-5) 

By contrast, `test_remove_entry` (which calls `remove_entry` directly, leaving links intact) correctly verifies that `ancestors_count` is decremented: [7](#0-6) 

`remove_entry_and_descendants` is called from three production paths: `resolve_conflict` (line 310), `resolve_conflict_header_dep`, and `check_and_record_ancestors` (line 618), all reachable by an unprivileged transaction submitter. [8](#0-7) [9](#0-8) 

## Impact Explanation
`EvictKey` is computed directly from the stale fields: [10](#0-9) 

An ancestor whose removed descendants had a higher fee rate than itself retains an inflated `descendants_feerate`, making `fee_rate` appear higher than it truly is. `next_evict_entry` selects the lowest-`EvictKey` entry to drop when the pool is full: [11](#0-10) 

This is called by `limit_size` whenever `total_tx_size > max_tx_pool_size`: [12](#0-11) 

The ancestor is ranked as more eviction-resistant than it deserves, so other legitimate transactions are evicted in its place. The stale state persists until the ancestor itself is removed or the pool is cleared. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**, as the mempool's internal eviction accounting is permanently corrupted for affected entries, causing incorrect prioritization of transactions for eviction.

## Likelihood Explanation
The trigger requires no privileged access, no majority hash power, and no social engineering. An attacker submits a low-fee root transaction X, then high-fee-rate descendants A→B spending X's output, then a conflicting transaction spending the same input as A. `resolve_conflict` calls `remove_entry_and_descendants(A)`, removing A and B while leaving X with inflated descendant stats. The cost is a single transaction fee. The attack is repeatable and can be applied to multiple ancestors simultaneously.

## Recommendation
Collect and apply ancestor updates **before** tearing down links, so `calc_ancestors` can still resolve them. Only update ancestors that are not themselves in the removal set:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();
    for removed_id in &removed_ids {
        if let Some(entry) = self.get(removed_id).cloned() {
            let ancestors = self.links.calc_ancestors(removed_id);
            for anc_id in ancestors.difference(&removed_set) {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&entry);
                    e.evict_key = e.inner.as_evict_key();
                });
            }
        }
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

Add a regression test asserting that after `remove_entry_and_descendants(&tx2_id)` on a chain `tx1 → tx2 → tx3`, `tx1`'s `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` equal their initial (self-only) values.

## Proof of Concept
**Setup**: pool contains chain `X → A → B`.

| Tx | fee | size | cycles |
|---|---|---|---|
| X | 100 | 100 | 100 |
| A | 300 | 200 | 200 |
| B | 200 | 200 | 200 |

After insertion, X's tracked state: `descendants_fee=600`, `descendants_size=500`, `descendants_cycles=500`, `descendants_count=3`.

**Trigger**: submit tx A′ spending the same input as A. `resolve_conflict` calls `remove_entry_and_descendants(A)`.

**Expected state of X**: `descendants_fee=100`, `descendants_size=100`, `descendants_cycles=100`, `descendants_count=1`.

**Actual state of X (bug)**: `descendants_fee=600`, `descendants_size=500`, `descendants_cycles=500`, `descendants_count=3`.

X's `EvictKey.fee_rate` is computed from the inflated `descendants_feerate = 600/500 = 1.2 shannons/KW` instead of the correct `100/100 = 1.0 shannons/KW`. When the pool is full, any transaction with a true fee rate between 1.0 and 1.2 is evicted before X, even though X should be the lower-priority entry. This is directly reproducible as a unit test extending `test_remove_entry_and_descendants` with assertions on `tx1`'s descendant fields after the call.

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

**File:** tx-pool/src/component/pool_map.rs (L252-264)
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
```

**File:** tx-pool/src/component/pool_map.rs (L305-332)
```rust
    pub(crate) fn resolve_conflict(&mut self, tx: &TransactionView) -> Vec<ConflictEntry> {
        let mut conflicts = Vec::new();

        for i in tx.input_pts_iter() {
            if let Some(id) = self.edges.remove_input(&i) {
                let entries = self.remove_entry_and_descendants(&id);
                if !entries.is_empty() {
                    let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                    let rejects = std::iter::repeat_n(reject, entries.len());
                    conflicts.extend(entries.into_iter().zip(rejects));
                }
            }

            // deps consumed
            if let Some(x) = self.edges.remove_deps(&i) {
                for id in x {
                    let entries = self.remove_entry_and_descendants(&id);
                    if !entries.is_empty() {
                        let reject = Reject::Resolve(OutPointError::Dead(i.clone()));
                        let rejects = std::iter::repeat_n(reject, entries.len());
                        conflicts.extend(entries.into_iter().zip(rejects));
                    }
                }
            }
        }

        conflicts
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

**File:** tx-pool/src/component/pool_map.rs (L588-625)
```rust
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
```

**File:** tx-pool/src/component/links.rs (L78-80)
```rust
    pub fn calc_ancestors(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.calc_relative_ids(short_id, Relation::Parents)
    }
```

**File:** tx-pool/src/component/entry.rs (L133-142)
```rust
    pub fn sub_descendant_weight(&mut self, entry: &TxEntry) {
        self.descendants_count = self.descendants_count.saturating_sub(1);
        self.descendants_size = self.descendants_size.saturating_sub(entry.size);
        self.descendants_cycles = self.descendants_cycles.saturating_sub(entry.cycles);
        self.descendants_fee = Capacity::shannons(
            self.descendants_fee
                .as_u64()
                .saturating_sub(entry.fee.as_u64()),
        );
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

**File:** tx-pool/src/component/tests/score_key.rs (L157-167)
```rust
    map.remove_entry(&tx1_id);
    assert!(!map.contains_key(&tx1_id));
    assert!(map.contains_key(&tx2_id));
    assert!(map.contains_key(&tx3_id));

    let tx3_entry = map.get(&tx3_id).unwrap();
    assert_eq!(tx3_entry.ancestors_count, 2);
    assert_eq!(
        map.calc_ancestors(&tx3_id),
        vec![tx2_id].into_iter().collect()
    );
```

**File:** tx-pool/src/component/tests/score_key.rs (L170-229)
```rust
#[test]
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
```

**File:** tx-pool/src/pool.rs (L298-325)
```rust
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
```
