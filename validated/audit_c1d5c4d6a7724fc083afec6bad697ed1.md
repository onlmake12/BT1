### Title
Stale `descendants_fee` Accounting in `remove_entry_and_descendants` Allows Tx-Pool Eviction Manipulation — (`tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries before calling `remove_entry` on each removed transaction. This prevents `update_ancestors_index_key` from locating the surviving ancestor transactions, so their `descendants_fee` (and related descendant weight fields) are never decremented. The stale, inflated `descendants_fee` on surviving ancestors corrupts the `EvictKey` used by `limit_size`, allowing an unprivileged attacker to keep low-fee transactions in the pool indefinitely and deny pool space to legitimate transactions.

### Finding Description

`remove_entry_and_descendants` is the primary removal path used by `resolve_conflict`, `resolve_conflict_header_dep`, `limit_size`, and `remove_by_detached_proposal`. [1](#0-0) 

The function first strips all link entries for every transaction in the removed subtree:

```rust
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
for id in &removed_ids {
    self.remove_entry_links(id);   // ← removes id from links map AND from parent's children set
}
```

`remove_entry_links` removes the entry from `TxLinksMap` and also removes the back-reference from each parent's `children` set: [2](#0-1) 

After this loop, `remove_entry` is called for each removed id. Inside `remove_entry`, `update_ancestors_index_key` is invoked to decrement the `descendants_fee` of surviving ancestors: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`: [4](#0-3) 

Because the link entry for the removed transaction was already deleted in the pre-removal loop, `calc_ancestors` returns an empty set. No surviving ancestor ever receives a `sub_descendant_weight` call. The `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields of every surviving ancestor remain permanently inflated.

The existing unit test `test_remove_entry_and_descendants` only verifies that the removed entries are gone and that `calc_descendants` is updated; it never asserts that `tx1.descendants_fee` is decremented: [5](#0-4) 

### Impact Explanation

`descendants_fee` feeds directly into `EvictKey.fee_rate`: [6](#0-5) 

`EvictKey` is the ordering key for pool eviction. A lower `fee_rate` is evicted first: [7](#0-6) 

`limit_size` repeatedly picks the entry with the smallest `EvictKey` and removes it: [8](#0-7) 

A transaction whose `descendants_fee` is stale-inflated appears to have a higher fee rate than it actually does, so it is never selected for eviction. Legitimate transactions with genuinely higher fee rates may be rejected with `Reject::Full` while the attacker's low-fee transaction persists indefinitely.

### Likelihood Explanation

The attack is reachable by any unprivileged RPC caller who can submit transactions. The required steps are:

1. Submit a low-fee transaction **A** (e.g., 1 shannon fee).
2. Submit a high-fee transaction **B** that spends **A**'s output AND a second input **X**. After insertion, **A**'s `descendants_fee` = A.fee + B.fee.
3. Submit a replacement transaction **B'** via RBF that spends input **X** but not **A**'s output. `resolve_conflict` calls `remove_entry_and_descendants(&B_id)`: [9](#0-8) 

4. **B** is removed but **A**'s `descendants_fee` is never decremented. **A** now carries stale inflated `descendants_fee` = A.fee + B.fee.
5. The pool is now full of **A** entries with artificially high eviction keys, blocking legitimate transactions.

RBF is an explicitly supported and tested feature of the CKB tx-pool: [10](#0-9) 

The attacker must pay the RBF replacement fee for **B'**, but this is a bounded, one-time cost per poisoned ancestor entry. The attack can be repeated to fill the pool with many such entries.

### Recommendation

Before removing link entries, iterate over the surviving ancestors of the root entry and call `sub_descendant_weight` for each entry in the removed subtree. Concretely, in `remove_entry_and_descendants`, collect the set of ancestors that are **not** in `removed_ids` before stripping links, then decrement their descendant weights:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();

    // Decrement descendant weights on surviving ancestors BEFORE links are removed
    for removed_id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(removed_id).map(|e| e.inner.clone()) {
            let ancestors = self.links.calc_ancestors(removed_id);
            for anc_id in ancestors {
                if !removed_set.contains(&anc_id) {
                    self.entries.modify_by_id(&anc_id, |e| {
                        e.inner.sub_descendant_weight(&entry);
                        e.evict_key = e.inner.as_evict_key();
                    });
                }
            }
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Alternatively, add a dedicated test asserting that after `remove_entry_and_descendants(&tx2_id)` in a chain `tx1 → tx2 → tx3`, `tx1.descendants_fee == tx1.fee` and `tx1.descendants_count == 1`.

### Proof of Concept

Given the chain `tx1 (fee=100) → tx2 (fee=200) → tx3 (fee=200)`:

1. Add all three to the pool. `tx1.descendants_fee = 500`.
2. Call `remove_entry_and_descendants(&tx2_id)`. `tx2` and `tx3` are removed.
3. Query `tx1.descendants_fee`. **Expected: 100. Actual: 500** (stale).
4. `tx1`'s `EvictKey.fee_rate` is computed from `descendants_fee=500` over `descendants_weight` for size+cycles of tx1+tx2+tx3, yielding a fee rate far above tx1's true rate of `100/weight(tx1)`.
5. When `limit_size` runs, `tx1` is never selected for eviction despite having a 1-shannon true fee rate, while legitimate transactions with 50-shannon fees are evicted instead.

The existing test at `tx-pool/src/component/tests/score_key.rs:170` can be extended to assert `tx1.descendants_fee == tx1.fee` after the removal to confirm the bug. [1](#0-0) [4](#0-3) [11](#0-10) [8](#0-7)

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

**File:** tx-pool/src/component/entry.rs (L234-248)
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
}
```

**File:** tx-pool/src/component/sort_key.rs (L92-104)
```rust
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

**File:** tx-pool/src/process.rs (L105-116)
```rust
                let conflicts = if tx_pool.enable_rbf() {
                    tx_pool.check_rbf(&snapshot, &entry)?
                } else {
                    // RBF is disabled but we found conflicts, return error here
                    // after_process will put this tx into conflicts_pool
                    let conflicted_outpoint =
                        tx_pool.pool_map.find_conflict_outpoint(entry.transaction());
                    if let Some(outpoint) = conflicted_outpoint {
                        return Err(Reject::Resolve(OutPointError::Dead(outpoint)));
                    }
                    HashSet::new()
                };
```
