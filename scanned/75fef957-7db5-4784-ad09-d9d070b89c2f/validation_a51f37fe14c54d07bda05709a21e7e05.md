### Title
Inflated Descendant-Weight Accounting in `remove_entry_and_descendants` Corrupts Tx-Pool Eviction Ordering — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records before calling `remove_entry` on each node. Because `update_ancestors_index_key` relies on those same link records to locate the surviving ancestors of the removed subtree root, the ancestors' cached `descendants_*` fields (`descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`) are never decremented. The result is a permanently inflated descendant-weight for every ancestor of any evicted or conflict-resolved subtree, corrupting the eviction key used to decide which transaction to drop when the pool is full.

---

### Finding Description

`remove_entry_and_descendants` first strips all link records for every node in the subtree, then calls `remove_entry` for each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips id from self.links entirely
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry` then calls `update_ancestors_index_key`, which looks up the ancestors of the removed entry through `self.links`:

```rust
// pool_map.rs  lines 432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id()); // ← empty: links already gone
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

Because `remove_entry_links` already removed the subtree root from `self.links`, `calc_ancestors` returns an empty set. The surviving ancestors (entries that are **not** being removed) never receive the `sub_descendant_weight` call, so their `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` remain at their pre-removal values indefinitely.

The comment on line 256 acknowledges the intent to skip `update_descendants_index_key` (updating the removed descendants' ancestor weights, which is harmless since those entries are gone), but the same pre-removal of links also silently disables `update_ancestors_index_key` for the surviving ancestors — the unintended side effect.

**Concrete scenario:**

| Step | Action | tx1.descendants_count (expected) | tx1.descendants_count (actual) |
|------|--------|----------------------------------|-------------------------------|
| Add tx1 | root | 1 | 1 |
| Add tx2 (child of tx1) | child | 2 | 2 |
| Add tx3 (child of tx2) | grandchild | 3 | 3 |
| `remove_entry_and_descendants(tx2)` | removes tx2+tx3 | **1** | **3** (bug) |

tx1 still reports 3 descendants even though it has none.

---

### Impact Explanation

The `descendants_*` fields feed directly into `EvictKey`:

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
            timestamp: entry.timestamp,
            descendants_count: entry.descendants_count,  // ← inflated
        }
    }
}
```

`next_evict_entry` iterates by `evict_key` to select the lowest-priority transaction to drop when the pool exceeds `max_tx_pool_size`:

```rust
// pool_map.rs  lines 380-385
pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
    self.entries
        .iter_by_evict_key()
        .find(move |entry| entry.status == status)
        .map(|entry| entry.id.clone())
}
```

With inflated `descendants_count` and `descendants_fee`, an ancestor whose subtree was already evicted appears to have more and higher-fee descendants than it actually does. This raises its apparent `fee_rate` in the eviction key, making it look more valuable than it is. As a result:

1. **Wrong transactions are evicted**: A legitimate high-fee transaction with an inflated eviction key may be skipped in favour of a genuinely low-fee transaction, or vice versa — the pool drops the wrong entry when it is full.
2. **Incorrect RPC data**: `get_raw_tx_pool` and `get_pool_tx_detail_info` return stale `descendants_size` / `descendants_cycles` values, misleading fee-estimation and monitoring tools.
3. **Repeated inflation**: Every conflict resolution (`resolve_conflict`), RBF replacement (`process_rbf`), header-dep invalidation (`resolve_conflict_header_dep`), and size-limit eviction (`limit_size`) calls `remove_entry_and_descendants`, so the inflation accumulates over the lifetime of the pool.

---

### Likelihood Explanation

Any unprivileged RPC caller or relay peer can trigger this path by:

1. Submitting a parent transaction tx_A via `send_transaction`.
2. Submitting a child tx_B (spending an output of tx_A).
3. Submitting a grandchild tx_C (spending an output of tx_B).
4. Submitting a conflicting transaction tx_D that spends the same input as tx_B.

Step 4 causes `resolve_conflict` → `remove_entry_and_descendants(tx_B)`, which removes tx_B and tx_C but leaves tx_A with inflated `descendants_*`. This is a normal, everyday pool operation requiring no special privilege. The inflation is permanent until tx_A itself is removed.

---

### Recommendation

Before stripping the links, capture the set of surviving ancestors of the subtree root and decrement their descendant weights explicitly:

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

Alternatively, restructure `remove_entry_and_descendants` to call `update_ancestors_index_key` for the subtree root before any links are removed, then suppress the redundant call inside `remove_entry` for the batch case.

---

### Proof of Concept

The existing test `test_remove_entry_and_descendants` in `tx-pool/src/component/tests/score_key.rs` (lines 170–230) removes tx2+tx3 from a chain tx1→tx2→tx3 but never asserts that tx1's `descendants_count` returns to 1. Adding the following assertion to that test demonstrates the bug:

```rust
// After map.remove_entry_and_descendants(&tx2_id):
let tx1_entry = map.get(&tx1_id).unwrap();
assert_eq!(tx1_entry.descendants_count, 1); // FAILS: actual value is 3
assert_eq!(tx1_entry.descendants_size, tx1_entry.size); // FAILS: still includes tx2+tx3 sizes
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

### Citations

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

**File:** tx-pool/src/pool.rs (L290-328)
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
