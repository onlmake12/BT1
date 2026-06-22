### Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Tx-Pool Eviction Manipulation — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` pre-removes all parent/child links for every entry in the removal set before calling `remove_entry` on each one. Because `remove_entry` relies on those same links to locate ancestors and decrement their `descendants_*` fields, the pre-removal of links silently skips the update. Any ancestor that is **not** in the removal set is left with permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`. The inflated values corrupt the `EvictKey` of those ancestors, making them appear to have a higher-fee descendant chain than they actually do, and therefore less likely to be evicted from the pool.

---

### Finding Description

`remove_entry_and_descendants` collects the target entry and all its descendants, then strips every link in that set before iterating and calling `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips ALL links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry` then calls `update_ancestors_index_key` to decrement the `descendants_*` fields of every ancestor of the entry being removed:

```rust
// tx-pool/src/component/pool_map.rs  lines 235-250
pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
    self.entries.remove_by_id(id).map(|entry| {
        self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);  // ← needs links
        self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
        ...
    })
}
```

`update_ancestors_index_key` resolves ancestors through the link graph:

```rust
// tx-pool/src/component/pool_map.rs  lines 432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← returns ∅ after links removed
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

Because `remove_entry_links` was already called for every entry in `removed_ids`, `calc_ancestors` returns an empty set for each of them. Ancestors that lie **outside** the removal set — i.e., entries that remain in the pool — never have `sub_descendant_weight` called on them. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are permanently inflated.

The comment in `remove_entry_and_descendants` acknowledges the intent ("so that we won't update_descendants_index_key in remove_entry") but the side-effect of also suppressing `update_ancestors_index_key` for surviving ancestors is unintended and undocumented.

---

### Impact Explanation

The `EvictKey` for each pool entry is derived from the (now-stale) descendant fields:

```rust
// tx-pool/src/component/entry.rs  lines 234-247
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
        }
    }
}
```

An ancestor entry whose `descendants_fee` is inflated (because a high-fee child was removed without decrementing) will report a falsely high `fee_rate` in its `EvictKey`. The pool's size-limit eviction loop (`limit_size` in `pool.rs:292-329`) selects the entry with the lowest `EvictKey` to evict first. A surviving ancestor with an inflated `EvictKey` is therefore systematically skipped during eviction, allowing it to occupy pool space indefinitely regardless of its actual fee rate.

---

### Likelihood Explanation

The trigger path is fully reachable by any unprivileged RPC caller via `send_transaction`:

1. Submit `tx_A` (low fee) — it enters the pool.
2. Submit `tx_B` (child of `tx_A`, high fee) — `tx_A.descendants_fee` is now `fee_A + fee_B`.
3. Submit `tx_C` (spends the same input as `tx_B`, any fee) — `resolve_conflict` calls `remove_entry_and_descendants(tx_B)`. `tx_A`'s `descendants_*` fields are never decremented.
4. `tx_A` now permanently carries the inflated `descendants_fee = fee_A + fee_B` even though `tx_B` no longer exists.

The same stale-state condition is triggered by every call site of `remove_entry_and_descendants`: `resolve_conflict`, `resolve_conflict_header_dep`, `process_rbf`, `limit_size`, and `remove_by_detached_proposal`. No special privilege is required; a standard RPC `send_transaction` call is sufficient.

---

### Recommendation

Before stripping links, explicitly update the `descendants_*` fields of all ancestors that are **not** in the removal set. One approach:

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

Alternatively, refactor `remove_entry` to accept an optional pre-computed ancestor set so that `remove_entry_and_descendants` can pass the correct surviving ancestors explicitly.

---

### Proof of Concept

**Setup**: Pool contains `tx1 → tx2 → tx3` (tx2 spends tx1's output, tx3 spends tx2's output).

After adding all three, `tx1` has:
- `descendants_count = 3`
- `descendants_fee = fee1 + fee2 + fee3`
- `descendants_size = size1 + size2 + size3`

**Trigger**: Submit `tx_conflict` that spends the same input as `tx2`. `resolve_conflict` calls `remove_entry_and_descendants(tx2_id)`.

Inside `remove_entry_and_descendants`:
- `removed_ids = [tx2, tx3]`
- `remove_entry_links(tx2)` and `remove_entry_links(tx3)` are called — all links gone.
- `remove_entry(tx2)` → `update_ancestors_index_key(tx2, Remove)` → `calc_ancestors(tx2)` returns `∅` → `tx1.sub_descendant_weight(tx2)` is **never called**.
- `remove_entry(tx3)` → same result.

**After**: `tx1` still reports `descendants_count=3`, `descendants_fee=fee1+fee2+fee3`, `descendants_size=size1+size2+size3`. Its `EvictKey.fee_rate` is computed from the ghost fees of `tx2` and `tx3`. When the pool fills and `limit_size` runs, `tx1` is ranked as if it still has two high-fee descendants and is skipped for eviction.

The existing test `test_resolve_conflict_descendants` (pending.rs:91-111) only asserts that `tx3` and `tx4` are absent from the pool after conflict resolution; it does not assert that `tx1`'s `descendants_*` fields are correctly decremented, confirming the regression is untested. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/component/entry.rs (L133-166)
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

    /// Update ancestor state for add an entry
    pub fn add_ancestor_weight(&mut self, entry: &TxEntry) {
        self.ancestors_count = self.ancestors_count.saturating_add(1);
        self.ancestors_size = self.ancestors_size.saturating_add(entry.size);
        self.ancestors_cycles = self.ancestors_cycles.saturating_add(entry.cycles);
        self.ancestors_fee = Capacity::shannons(
            self.ancestors_fee
                .as_u64()
                .saturating_add(entry.fee.as_u64()),
        );
    }

    /// Update ancestor state for remove an entry
    pub fn sub_ancestor_weight(&mut self, entry: &TxEntry) {
        self.ancestors_count = self.ancestors_count.saturating_sub(1);
        self.ancestors_size = self.ancestors_size.saturating_sub(entry.size);
        self.ancestors_cycles = self.ancestors_cycles.saturating_sub(entry.cycles);
        self.ancestors_fee = Capacity::shannons(
            self.ancestors_fee
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
