### Title
Stale Ancestor `evict_key` After Batch Removal in `remove_entry_and_descendants` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries for every transaction being removed **before** calling `remove_entry` on each one. Because `remove_entry` relies on the link graph to find and update ancestor entries, the pre-removal causes `update_ancestors_index_key` to find an empty ancestor set and silently skip updating the `descendants_*` accounting fields and `evict_key` of ancestor transactions that remain in the pool. This is the direct CKB analog of the PositionManager bug: computed updated values are used (or in this case, the update function is called) but the underlying state is never written back.

---

### Finding Description

`remove_entry_and_descendants` collects the root ID and all its descendants, then calls `remove_entry_links` for **every** ID in that set before iterating to call `remove_entry`: [1](#0-0) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);          // ← ALL links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← remove_entry called after links are gone
        .collect()
}
```

Inside `remove_entry`, the first thing done is: [2](#0-1) 

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` walks the link graph to find all ancestors of the removed entry and decrements their `descendants_*` fields and recomputes their `evict_key`: [3](#0-2) 

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

Because `remove_entry_links` was already called for the root entry, `calc_ancestors` returns an **empty set**. The loop body never executes. Ancestor transactions that remain in the pool are never updated: their `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and `evict_key` all retain stale, inflated values.

By contrast, single-entry `remove_entry` calls `remove_entry_links` **after** the update functions, so the link graph is intact when ancestors are looked up — the bug is specific to the batch path. [4](#0-3) 

The `EvictKey` is derived from `descendants_fee` and `descendants_size/cycles`: [5](#0-4) 

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
            descendants_count: entry.descendants_count,
        }
    }
}
```

An ancestor whose high-fee child was removed will still carry the child's fee in its `descendants_fee`, producing an inflated `fee_rate` in its `evict_key`. The pool eviction loop uses `evict_key` ordering directly: [6](#0-5) 

The existing test `test_remove_entry_and_descendants` only asserts that the removed entries are gone and that `calc_descendants` returns the correct set; it never checks that the remaining ancestor's `descendants_count` or `evict_key` are updated, so the bug is untested. [7](#0-6) 

---

### Impact Explanation

After `remove_entry_and_descendants` is triggered, any ancestor transaction remaining in the pool carries a stale, inflated `evict_key`. When the pool reaches its size limit and `limit_size` iterates `next_evict_entry` to select victims, the stale ancestor appears to have a higher descendants-fee-rate than it actually does, so it is ranked as a lower-priority eviction candidate. Legitimate higher-fee transactions submitted later may be evicted in its place. This corrupts the pool's eviction fairness guarantee and can be used to keep low-fee transactions alive in the pool at the expense of higher-fee ones.

---

### Likelihood Explanation

`remove_entry_and_descendants` is called from multiple hot paths: `resolve_conflict` (triggered on every new transaction submission that conflicts with a pool entry), `resolve_conflict_header_dep` (triggered on every new block), `remove_by_detached_proposal`, and `limit_size` itself. Any transaction chain of depth ≥ 2 where the child is later removed will trigger the stale-state condition. An unprivileged tx-pool submitter can reliably reproduce this by submitting a parent–child transaction pair and then submitting a conflicting transaction to evict the child.

---

### Recommendation

Move the `remove_entry_links` pre-pass so that it only covers the **descendants** (not the root entry), or update ancestor state before tearing down links. Concretely, before calling `remove_entry_links` for the root `id`, call `update_ancestors_index_key` for the root entry while the link graph is still intact. Alternatively, restructure `remove_entry_and_descendants` to collect and update all affected ancestors first, then bulk-remove links and entries.

The existing unit test should be extended to assert that after `remove_entry_and_descendants(&tx2_id)`, `tx1.descendants_count == 1` and `tx1.evict_key` reflects only `tx1`'s own fee.

---

### Proof of Concept

```
1. Build a PoolMap with max_ancestors_count = 25.
2. Insert tx1 (fee=100, size=100) as Pending.
3. Insert tx2 (fee=10_000, size=100) spending tx1's output as Pending.
   → tx1.descendants_fee = 10_100, tx1.evict_key.fee_rate is high.
4. Call pool_map.remove_entry_and_descendants(&tx2_id).
   → tx2 is removed. tx1 remains.
5. Assert tx1.descendants_count == 1  ← FAILS, still 2
   Assert tx1.descendants_fee == 100  ← FAILS, still 10_100
   Assert tx1.evict_key reflects only tx1's own fee ← FAILS
6. Insert tx3 (fee=500, size=100) as Pending (legitimate high-fee tx).
7. Fill pool to max_tx_pool_size, triggering limit_size.
   → tx3 is evicted before tx1 because tx1's stale evict_key shows
     descendants_feerate >> tx3's actual feerate.
``` [1](#0-0) [3](#0-2) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/pool.rs (L298-326)
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
        }
```

**File:** tx-pool/src/component/tests/score_key.rs (L170-230)
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
}
```
