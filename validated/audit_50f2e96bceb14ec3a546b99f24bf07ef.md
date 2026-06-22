### Title
Ancestor Descendant-Weight Stats Permanently Inflated After `remove_entry_and_descendants` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-clears all link records before calling `remove_entry` on each evicted transaction. Because `update_ancestors_index_key` resolves surviving ancestors through those same link records, the decrement (`sub_descendant_weight`) is never applied to any ancestor that remains in the pool. The add path (`add_descendant_weight`) always runs correctly. The result is a permanent, monotonically-growing overcount of `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` on surviving ancestor entries, which corrupts eviction ordering and allows an unprivileged tx-pool submitter to pin a low-fee transaction in the pool indefinitely.

---

### Finding Description

**Add path (correct):**

When a child transaction is inserted, `record_entry_descendants` calls `update_ancestors_index_key(entry, EntryOp::Add)`, which resolves all ancestors through the live link graph and calls `add_descendant_weight` on each one. [1](#0-0) [2](#0-1) [3](#0-2) 

**Remove path (broken):**

`remove_entry_and_descendants` first strips every link for every entry in the removal set, then calls `remove_entry` on each:

```
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
for id in &removed_ids {
    self.remove_entry_links(id);   // ← wipes the link graph for ALL removed entries
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
``` [4](#0-3) 

Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`: [5](#0-4) 

`update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors(&child.proposal_short_id())`. Because the links were already removed in the pre-clearing loop, `calc_ancestors` returns an empty set. `sub_descendant_weight` is therefore **never called** on any surviving ancestor: [2](#0-1) [6](#0-5) 

The developer comment says the pre-clearing is intended to prevent `update_descendants_index_key` from touching entries that are themselves being removed. That is correct, but the same pre-clearing also silences `update_ancestors_index_key` for entries that are **not** being removed, which is the bug.

The existing test `test_remove_entry_and_descendants` only asserts that the removed entries are gone from the pool and from the link graph; it does not assert that the surviving ancestor's `descendants_fee/size/cycles/count` fields were decremented: [7](#0-6) 

---

### Impact Explanation

The `EvictKey` for each entry is computed from `descendants_feerate` (derived from `descendants_fee` and `descendants_size/cycles`) and `descendants_count`: [8](#0-7) [9](#0-8) 

`next_evict_entry` selects the entry with the **lowest** `EvictKey` (lowest descendant fee rate, then fewest descendants, then oldest): [10](#0-9) 

With inflated `descendants_fee` and `descendants_count`, a surviving ancestor appears to have more and higher-fee descendants than it actually does. Its `EvictKey` is therefore artificially elevated, making it resistant to eviction even when the pool is full. `limit_size` uses `remove_entry_and_descendants` on the chosen eviction target, which compounds the problem by further inflating other survivors' stats: [11](#0-10) 

---

### Likelihood Explanation

The trigger is `resolve_conflict`, which is called on every new transaction submission that spends an input already consumed by a pooled transaction. This is a normal, unprivileged operation available to any tx-pool submitter via the RPC or P2P relay. No special role, key, or majority hashpower is required. The attacker needs only to submit a parent transaction and then repeatedly submit conflicting children to keep the parent's descendant stats inflated. [12](#0-11) 

---

### Recommendation

Before pre-clearing links, identify which ancestors are **not** in the removal set and apply `sub_descendant_weight` to them directly. Concretely:

1. Collect the full removal set (target + all descendants).
2. For each entry in the removal set, compute its ancestors via the still-intact link graph.
3. For each ancestor that is **not** in the removal set, call `sub_descendant_weight` with the entry being removed.
4. Only then clear links and proceed with `remove_entry`.

Alternatively, after the batch removal, recompute `descendants_*` for all surviving entries that were ancestors of any removed entry, similar to how `update_stat_for_remove_tx` falls back to `recompute_total_stat` on underflow. [13](#0-12) 

---

### Proof of Concept

```
State: pool is near max_tx_pool_size

1. Attacker submits tx_A (low fee, e.g. 1 shannon/byte) → accepted.
   tx_A.descendants_fee = 1 shannon, descendants_count = 1

2. Attacker submits tx_B (child of tx_A, high fee, e.g. 1000 shannons) → accepted.
   tx_A.descendants_fee += 1000  →  1001 shannons, descendants_count = 2

3. Attacker submits tx_C (child of tx_B, high fee, e.g. 1000 shannons) → accepted.
   tx_A.descendants_fee += 1000  →  2001 shannons, descendants_count = 3

4. Attacker submits tx_B' (spends same input as tx_B, any fee).
   resolve_conflict → remove_entry_and_descendants(tx_B):
     - remove_entry_links(tx_B), remove_entry_links(tx_C)   ← links gone
     - remove_entry(tx_B): calc_ancestors(tx_B) = {}        ← tx_A NOT updated
     - remove_entry(tx_C): calc_ancestors(tx_C) = {}        ← tx_A NOT updated

5. After step 4:
   tx_A.descendants_fee  = 2001 shannons  (should be 1)
   tx_A.descendants_count = 3             (should be 1)
   tx_A's EvictKey shows high descendant fee rate → tx_A is never chosen for eviction.

6. Repeat steps 2–4 indefinitely to keep tx_A's stats inflated.
   Pool fills with legitimate high-fee transactions, but tx_A is never evicted,
   blocking pool space and potentially causing legitimate transactions to be rejected.
``` [4](#0-3) [2](#0-1) [14](#0-13)

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

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
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

**File:** tx-pool/src/component/sort_key.rs (L79-104)
```rust
#[derive(Eq, PartialEq, Clone, Debug)]
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
