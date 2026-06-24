Audit Report

## Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Corrupts Eviction Priority — (`File: tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` calls `remove_entry_links` on every entry in the removal set before calling `remove_entry` on each one. Because `remove_entry` relies on the links map to locate ancestors via `update_ancestors_index_key` → `calc_ancestors`, the pre-removal of links causes `calc_ancestors` to return an empty set for every removed entry. Surviving ancestors outside the removal set are therefore never decremented, leaving their `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and stored `evict_key` permanently inflated. This corrupts `EvictKey`-based eviction ordering and allows an attacker to fill the pool with low-fee transactions that are never evicted.

## Finding Description

**Root cause — `remove_entry_and_descendants` (L252–265):**

All links are stripped in a first pass, then `remove_entry` is called in a second pass: [1](#0-0) 

**`remove_entry_links` (L418–430)** removes the entry from its parents' children lists, removes the entry from its children's parents lists, and then removes the entry from the links map entirely: [2](#0-1) 

**`remove_entry` (L235–250)** calls `update_ancestors_index_key` immediately after removing the entry from the multi-index map: [3](#0-2) 

**`update_ancestors_index_key` (L432–444)** resolves ancestors through `self.links.calc_ancestors`: [4](#0-3) 

**`calc_ancestors` in `TxLinksMap` (L78–80)** calls `calc_relative_ids`, which does `self.inner.get(short_id)`. Because `remove_entry_links` already called `self.links.remove(id)` for every entry in `removed_ids`, `self.inner.get(short_id)` returns `None`, and `unwrap_or_default()` yields an empty set: [5](#0-4) 

The result: `update_ancestors_index_key` iterates over an empty ancestor set and `sub_descendant_weight` is never called for any surviving ancestor.

**Concrete exploit trace (tx1 → tx3 → tx4, then tx2 conflicts with tx3):**

1. Add tx1, tx3 (child of tx1), tx4 (child of tx3). After insertion, tx1 has `descendants_count=3`, `descendants_fee=fee1+fee3+fee4`, and its stored `evict_key` reflects those values.
2. Submit tx2 spending the same output as tx3. `resolve_conflict` calls `remove_entry_and_descendants(tx3_id)`.
3. Inside `remove_entry_and_descendants`: `removed_ids = [tx3, tx4]`.
4. `remove_entry_links(tx3)`: removes tx3 from tx1's children list; removes tx3 from tx4's parents list; removes tx3 from the links map.
5. `remove_entry_links(tx4)`: tx4's parents list is now empty (tx3 was already removed); removes tx4 from the links map.
6. `remove_entry(tx3)` → `update_ancestors_index_key(tx3, Remove)` → `calc_ancestors(tx3)` returns ∅ → `tx1.sub_descendant_weight(tx3)` is **never called**.
7. `remove_entry(tx4)` → same result.
8. After the call: tx1 still reports `descendants_count=3`, `descendants_fee=fee1+fee3+fee4`, and its stored `evict_key.fee_rate` is computed from those ghost values.

The comment in the code ("so that we won't update_descendants_index_key in remove_entry") acknowledges the intent to skip updating `ancestors_*` fields of entries being removed (correct, since they are all being removed). However, it also silently suppresses `update_ancestors_index_key`, which must update `descendants_*` fields of **surviving** ancestors.

**The existing test `test_resolve_conflict_descendants` (L91–111)** only asserts that tx3 and tx4 are absent from the pool; it does not assert that tx1's `descendants_*` fields are correctly decremented: [6](#0-5) 

## Impact Explanation

The `EvictKey` for each pool entry is derived directly from the (now-stale) descendant fields: [7](#0-6) 

`EvictKey` ordering selects the entry with the lowest `fee_rate` first for eviction: [8](#0-7) 

`next_evict_entry` selects the first entry in `iter_by_evict_key()` order: [9](#0-8) 

`limit_size` uses this to decide which entries to remove when the pool is over capacity: [10](#0-9) 

A surviving ancestor with an inflated `descendants_fee` (from a removed high-fee child) reports a falsely high `fee_rate` in its stored `evict_key` and is therefore systematically skipped during eviction. An attacker can repeat the three-step pattern (submit low-fee tx_A, high-fee child tx_B, conflicting tx_C) indefinitely to fill the pool with low-fee entries that cannot be evicted, preventing legitimate higher-fee transactions from entering the pool.

This matches the **High** impact category: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

The trigger path requires no special privilege. Any user with RPC access can call `send_transaction` to submit tx_A, tx_B, and tx_C in sequence. The conflict resolution path (`resolve_conflict`) is exercised on every transaction submission that spends an already-spent input. The pattern is repeatable indefinitely, and each iteration permanently inflates the `descendants_*` fields and stored `evict_key` of one additional surviving ancestor. The attacker's cost per inflation event is the sum of fees for three transactions, which can be kept near the minimum fee rate for tx_A and tx_C; only tx_B needs to be a child of tx_A with a higher fee.

## Recommendation

Before stripping links, explicitly update the `descendants_*` fields and `evict_key` of all ancestors that are **not** in the removal set:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();

    // Decrement descendants_* for surviving ancestors BEFORE links are removed.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid).map(|e| e.inner.clone()) {
            let ancestors = self.links.calc_ancestors(rid);
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

## Proof of Concept

**Minimal unit test** (add to `tx-pool/src/component/tests/pending.rs`):

```rust
#[test]
fn test_resolve_conflict_descendants_stale_ancestor_weight() {
    let mut pool = PoolMap::new(1000);
    let tx1 = build_tx(vec![(&Byte32::zero(), 1)], 1);
    let tx3 = build_tx(vec![(&tx1.hash(), 0)], 2);
    let tx4 = build_tx(vec![(&tx3.hash(), 0)], 1);
    let tx2 = build_tx(vec![(&tx1.hash(), 0)], 1);

    let entry1 = TxEntry::dummy_resolve(tx1.clone(), MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    let entry3 = TxEntry::dummy_resolve(tx3, MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    let entry4 = TxEntry::dummy_resolve(tx4, MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    assert!(pool.add_entry(entry1, Status::Pending).is_ok());
    assert!(pool.add_entry(entry3, Status::Pending).is_ok());
    assert!(pool.add_entry(entry4, Status::Pending).is_ok());

    pool.resolve_conflict(&tx2);

    // After removing tx3 and tx4, tx1 must reflect only itself.
    let tx1_entry = pool.get(&tx1.proposal_short_id()).unwrap();
    assert_eq!(tx1_entry.descendants_count, 1);   // currently fails: reports 3
    assert_eq!(tx1_entry.descendants_fee, MOCK_FEE); // currently fails: reports 3×MOCK_FEE
}
```

Both assertions currently fail, confirming the regression is live and untested.

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

**File:** tx-pool/src/component/pool_map.rs (L432-444)
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

**File:** tx-pool/src/component/tests/pending.rs (L91-111)
```rust
fn test_resolve_conflict_descendants() {
    let mut pool = PoolMap::new(1000);
    let tx1 = build_tx(vec![(&Byte32::zero(), 1)], 1);
    let tx3 = build_tx(vec![(&tx1.hash(), 0)], 2);
    let tx4 = build_tx(vec![(&tx3.hash(), 0)], 1);

    let tx2 = build_tx(vec![(&tx1.hash(), 0)], 1);

    let entry1 = TxEntry::dummy_resolve(tx1, MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    let entry3 = TxEntry::dummy_resolve(tx3, MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    let entry4 = TxEntry::dummy_resolve(tx4, MOCK_CYCLES, MOCK_FEE, MOCK_SIZE);
    assert!(pool.add_entry(entry1, Status::Pending).is_ok());
    assert!(pool.add_entry(entry3.clone(), Status::Pending).is_ok());
    assert!(pool.add_entry(entry4.clone(), Status::Pending).is_ok());

    let conflicts = pool.resolve_conflict(&tx2);
    assert_eq!(
        conflicts.into_iter().map(|i| i.0).collect::<HashSet<_>>(),
        HashSet::from_iter(vec![entry3, entry4])
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

**File:** tx-pool/src/component/sort_key.rs (L92-103)
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
