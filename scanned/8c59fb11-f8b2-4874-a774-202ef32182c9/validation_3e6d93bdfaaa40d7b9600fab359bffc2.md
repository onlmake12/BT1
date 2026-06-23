### Title
Stale `descendants_*` State in Ancestor `TxEntry` After `remove_entry_and_descendants` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

When `remove_entry_and_descendants` removes a transaction subtree from the tx-pool, it pre-removes all parent-child links before calling `remove_entry` on each node. As a result, `update_ancestors_index_key` — which relies on those links to find and update ancestor entries — silently becomes a no-op. Ancestor entries that remain in the pool are left with inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and a stale `EvictKey`. Any subsequent addition of new descendants accumulates on top of these stale values, mirroring the Fenwick-tree partial-reset bug exactly.

---

### Finding Description

**Root cause — `remove_entry_and_descendants` pre-removes links before updating ancestors** [1](#0-0) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);          // ← ALL parent-child links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← remove_entry called AFTER links gone
        .collect()
}
```

The comment acknowledges that pre-removing links prevents `update_descendants_index_key` from running (intentional — descendants are being removed anyway). However, it also silently prevents `update_ancestors_index_key` from running, which is **not** intentional.

**Inside `remove_entry`, the ancestor update is now a no-op** [2](#0-1) 

`remove_entry` calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`: [3](#0-2) 

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← returns ∅ because links already gone
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

Because `remove_entry_links` already severed the parent→child edge, `calc_ancestors` returns an empty set. The loop body never executes. The ancestor's `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and `evict_key` are **never decremented**.

**Stale state accumulates on subsequent additions**

When a new child is later added to the same ancestor, `add_descendant_weight` is called: [4](#0-3) 

This adds on top of the already-inflated counters, compounding the error — exactly the Fenwick-tree pattern where new deposits accumulate on top of dirty indices.

**The `EvictKey` is computed from the stale fields** [5](#0-4) 

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        ...
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),
            ...
        }
    }
}
```

An ancestor with inflated `descendants_fee` will have an artificially high `EvictKey.fee_rate`, making it appear more valuable than it actually is.

**`limit_size` and `next_evict_entry` use this stale key** [6](#0-5) [7](#0-6) 

When the pool is full, `limit_size` calls `next_evict_entry`, which iterates by `EvictKey` to find the lowest-fee-rate entry to evict. An ancestor with a stale inflated key is skipped, and a legitimate higher-fee transaction may be evicted instead.

**The existing test does not catch this** [8](#0-7) 

The test for `remove_entry_and_descendants` only asserts that tx2/tx3 are gone and that `calc_descendants(tx1)` is empty. It never checks `tx1.descendants_fee`, `tx1.descendants_count`, or `tx1.evict_key` after the removal.

---

### Impact Explanation

**Impact: High**

An attacker who controls tx submission can permanently inflate the `EvictKey` of any ancestor transaction they own:

1. Submit `tx_A` (low fee rate, pending).
2. Submit `tx_B` (child of `tx_A`, high fee rate) — `tx_A.descendants_fee` is now inflated.
3. Submit `tx_C` that double-spends one of `tx_B`'s inputs. `resolve_conflict` calls `remove_entry_and_descendants(tx_B)`.
4. `tx_A.descendants_fee` is **not** decremented; `tx_A.evict_key` still reflects `tx_B`'s high fee.
5. When the pool is full and `limit_size` runs, `tx_A` is skipped for eviction because its `EvictKey` appears high.
6. Legitimate high-fee transactions submitted by other users are evicted instead.
7. Repeat steps 2–4 to re-inflate after any natural decay.

This allows a tx-pool submitter to occupy pool space with low-fee transactions indefinitely, causing denial-of-service against other users' transactions and degrading miner revenue by displacing higher-fee transactions.

---

### Likelihood Explanation

**Likelihood: High**

- No special privilege is required — any node that can submit transactions to the tx-pool can trigger this.
- `remove_entry_and_descendants` is called from multiple reachable paths: `resolve_conflict` (triggered by any conflicting tx submission), `resolve_conflict_header_dep` (triggered by block relay), `limit_size` (triggered by pool pressure), `remove_by_detached_proposal` (triggered by normal chain operation), and `check_and_record_ancestors` (triggered by ancestor-count eviction).
- The attacker needs only two transactions and one conflicting transaction — a trivial setup.
- The stale state persists until the ancestor is itself removed or the pool is cleared.

---

### Recommendation

Before pre-removing links in `remove_entry_and_descendants`, collect and update the ancestors of the root entry being removed. Specifically, before the `remove_entry_links` loop, iterate over the ancestors of `id` (the root of the subtree) and call `sub_descendant_weight` for each entry in the subtree that is being removed:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors of the root before links are torn down
    let ancestors = self.links.calc_ancestors(id);
    for removed_id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(removed_id) {
            let inner = entry.inner.clone();
            for anc_id in &ancestors {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&inner);
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

Alternatively, add a dedicated `update_ancestors_for_subtree_removal` method that is called before link teardown, and add a regression test that asserts `tx1.descendants_fee == tx1.fee` after `remove_entry_and_descendants(&tx2_id)`.

---

### Proof of Concept

```
Pool state:
  tx1 (fee=100, pending) → tx2 (fee=500, pending) → tx3 (fee=200, pending)

After add_proposed(tx1), add_proposed(tx2), add_proposed(tx3):
  tx1.descendants_fee = 100 + 500 + 200 = 800
  tx1.evict_key.fee_rate = high (due to tx2+tx3)

Attacker submits tx2' that double-spends tx2's input.
resolve_conflict calls remove_entry_and_descendants(tx2).

Expected after removal:
  tx1.descendants_fee = 100  (only itself)
  tx1.evict_key.fee_rate = low

Actual after removal (bug):
  tx1.descendants_fee = 800  (stale — tx2 and tx3 never subtracted)
  tx1.evict_key.fee_rate = high (stale)

Pool fills up. limit_size calls next_evict_entry(Pending).
iter_by_evict_key() skips tx1 because its EvictKey.fee_rate is artificially high.
A legitimate high-fee transaction submitted by another user is evicted instead.

Attacker re-submits tx2 (fee=500) as a new child of tx1.
tx1.descendants_fee = 800 + 500 = 1300  (accumulates on stale base)
Repeat to inflate indefinitely.
```

The existing test at `tx-pool/src/component/tests/score_key.rs:170` confirms the setup is valid but does not assert `tx1.descendants_fee` after removal, leaving the bug undetected. [1](#0-0) [3](#0-2) [9](#0-8) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L235-249)
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

**File:** tx-pool/src/component/entry.rs (L121-142)
```rust
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

    /// Update ancestor state for remove an entry
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

**File:** tx-pool/src/pool.rs (L292-328)
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
