### Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Allows Eviction-Priority Inflation — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries before calling `remove_entry` on each member of the subtree. Because `remove_entry` relies on the live link graph to find ancestors and update their `descendants_*` fields, pre-removing the links silently skips those updates. The ancestors of the removed subtree are left with permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`. An unprivileged tx-pool submitter can exploit this to make a low-fee transaction appear highly valuable, protecting it from eviction and potentially displacing legitimate transactions.

---

### Finding Description

`remove_entry_and_descendants` first strips all link entries for every transaction in the subtree, then calls `remove_entry` on each:

```
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips ALL link entries first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
``` [1](#0-0) 

Inside `remove_entry`, the ancestor-update path is:

```
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
``` [2](#0-1) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(child_id)` to discover which pool entries should have their `descendants_*` decremented:

```
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),
                ...
            };
        });
    }
}
``` [3](#0-2) 

Because `remove_entry_links` already erased the link entry for the removed transaction, `calc_ancestors` returns an empty set. The ancestors of the removed subtree root — which remain in the pool — never receive the `sub_descendant_weight` call. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` are permanently inflated. [4](#0-3) 

The existing test `test_remove_entry_and_descendants` only asserts that the removed entries are gone and that `calc_descendants` returns an empty set; it never checks the surviving ancestor's `descendants_*` fields, so the bug is undetected. [5](#0-4) 

---

### Impact Explanation

The `EvictKey` for every surviving ancestor is computed from the stale `descendants_fee` and `descendants_size`:

```
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
let feerate = FeeRate::calculate(entry.fee, weight);
EvictKey {
    fee_rate: descendants_feerate.max(feerate),
    ...
}
``` [6](#0-5) 

A transaction with inflated `descendants_feerate` is ranked as more valuable during pool-full eviction (`limit_size`), so it is evicted last. Legitimate high-fee transactions submitted by other users may be evicted in its place. The inflation is additive across repeated cycles: each removal-and-resubmission of a child transaction adds another copy of the child's fee to the ancestor's stale `descendants_fee`, allowing unbounded amplification. [7](#0-6) 

---

### Likelihood Explanation

The attack requires only the ability to submit transactions to the tx-pool, which is available to any unprivileged peer or RPC caller. Two concrete trigger paths exist without any privileged access:

1. **RBF path** (when `min_rbf_rate > min_fee_rate`): submit a child tx, then replace it with a higher-fee version. `process_rbf` calls `remove_entry_and_descendants` on the old child, leaving the parent's `descendants_*` inflated. The new child is then added, further inflating the parent. [8](#0-7) 

2. **Conflict path**: submit a second transaction that spends the same input as the child. `resolve_conflict` calls `remove_entry_and_descendants` on the child, again leaving the parent's `descendants_*` inflated. [9](#0-8) 

Each cycle costs the attacker only the incremental RBF fee premium, while the apparent `descendants_feerate` of the parent grows without bound.

---

### Recommendation

Before pre-removing links in `remove_entry_and_descendants`, explicitly update the `descendants_*` fields of the surviving ancestors of the subtree root. Concretely, before the link-removal loop, collect the ancestors of `id` (the subtree root) and call `sub_descendant_weight` on each of them for every entry being removed. Alternatively, restructure `remove_entry` so that ancestor updates are performed using a snapshot of the link graph taken before any links are torn down.

---

### Proof of Concept

Consider three transactions in the pool: `tx1 → tx2 → tx3` (tx2 spends tx1's output, tx3 spends tx2's output).

1. Submit `tx1` (fee = 100, size = 100).
2. Submit `tx2` (fee = 200, size = 200) spending `tx1`'s output.
3. Submit `tx3` (fee = 200, size = 200) spending `tx2`'s output.
4. After insertion, `tx1.descendants_fee = 500`, `tx1.descendants_count = 3`.
5. Call `remove_entry_and_descendants(tx2_id)` (triggered by RBF or conflict).
   - Links for `tx2` and `tx3` are pre-removed.
   - `remove_entry(tx2)`: `calc_ancestors(tx2)` → empty (links gone) → `tx1.descendants_*` NOT updated.
   - `remove_entry(tx3)`: same.
6. `tx1.descendants_fee` remains `500` (should be `100`), `tx1.descendants_count` remains `3` (should be `1`).
7. Submit a new `tx2'` spending `tx1`'s output (fee = 200).
   - `add_descendant_weight` is called on `tx1`: `tx1.descendants_fee += 200` → now `700`.
8. `tx1`'s `EvictKey` reflects `descendants_feerate` based on fee=700/size=700, far above its true value of fee=100/size=100.
9. Repeat steps 5–8 to amplify without bound. [10](#0-9) [11](#0-10)

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

**File:** tx-pool/src/component/pool_map.rs (L487-513)
```rust
    fn record_entry_descendants(&mut self, entry: &TxEntry) {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let outputs = entry.transaction().output_pts();
        let mut children = HashSet::new();

        // collect children
        for o in outputs {
            if let Some(ids) = self.edges.get_deps_ref(&o).cloned() {
                children.extend(ids);
            }
            if let Some(id) = self.edges.get_input_ref(&o).cloned() {
                children.insert(id);
            }
        }
        // update children
        if !children.is_empty() {
            for child in &children {
                self.links.add_parent(child, tx_short_id.clone());
            }
            if let Some(links) = self.links.inner.get_mut(&tx_short_id) {
                links.children.extend(children);
            }
            self.update_descendants_index_key(entry, EntryOp::Add);
        }
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
    }
```

**File:** tx-pool/src/component/entry.rs (L120-130)
```rust
    /// Update ancestor state for add an entry
    pub fn add_descendant_weight(&mut self, entry: &TxEntry) {
        self.descendants_count = self.descendants_count.saturating_add(1);
        self.descendants_size = self.descendants_size.saturating_add(entry.size);
        self.descendants_cycles = self.descendants_cycles.saturating_add(entry.cycles);
        self.descendants_fee = Capacity::shannons(
            self.descendants_fee
                .as_u64()
                .saturating_add(entry.fee.as_u64()),
        );
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

**File:** tx-pool/src/pool.rs (L290-329)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
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

**File:** tx-pool/src/process.rs (L190-234)
```rust
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
```
