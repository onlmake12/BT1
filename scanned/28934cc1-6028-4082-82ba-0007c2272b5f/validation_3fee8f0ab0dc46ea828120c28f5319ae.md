### Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Tx-Pool Eviction Manipulation — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records from `self.links` before calling `remove_entry` on each entry. Because `update_ancestors_index_key` inside `remove_entry` queries `self.links` to find ancestors, it finds nothing and silently skips updating the remaining ancestors' `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields. Those fields are permanently inflated. An unprivileged tx-pool submitter can exploit this via RBF to make low-fee transactions appear to have high descendant fee-rates, defeating the pool's eviction mechanism and enabling a sustained tx-pool DoS.

---

### Finding Description

**Root cause — `remove_entry_and_descendants`** [1](#0-0) 

The function first calls `remove_entry_links` for every entry in the subtree (root + all descendants): [2](#0-1) 

`remove_entry_links` removes the entry from `self.links` entirely and also removes it from its parents' children-sets: [3](#0-2) 

Only after all links are torn down does the function call `remove_entry` for each id. Inside `remove_entry`, `update_ancestors_index_key` is called with `EntryOp::Remove`: [4](#0-3) 

`update_ancestors_index_key` resolves ancestors by querying `self.links`: [5](#0-4) 

`calc_ancestors` calls `calc_relative_ids`, which looks up the entry in `self.links.inner`: [6](#0-5) 

Because `remove_entry_links` already removed the root entry from `self.links.inner`, `calc_ancestors` returns an empty set. The loop body never executes. The ancestors that remain in the pool never receive `sub_descendant_weight(child)` and their `evict_key` is never refreshed.

**What stays stale**

Each `TxEntry` tracks: [7](#0-6) 

These are used to compute the `EvictKey`: [8](#0-7) 

`fee_rate` in `EvictKey` is `max(descendants_feerate, feerate)`. After the subtree is removed, the ancestor's `descendants_fee` and `descendants_size` still include the removed children's contributions, so `descendants_feerate` is permanently inflated.

**The existing test does not catch this**

The only test for `remove_entry_and_descendants` checks that the removed entries are gone and that `calc_descendants` returns the correct set, but never asserts that the surviving ancestor's `descendants_*` fields are correct: [9](#0-8) 

---

### Impact Explanation

The `EvictKey` determines which transactions are evicted when the pool exceeds `max_tx_pool_size` (default 180 MB): [10](#0-9) 

`next_evict_entry` picks the entry with the lowest `EvictKey` (lowest effective fee-rate): [11](#0-10) 

An ancestor with inflated `descendants_feerate` sorts higher (less evictable) than it should. An attacker can therefore keep arbitrarily low-fee transactions in a full pool indefinitely, preventing legitimate higher-fee transactions from entering. This is a tx-pool resource-exhaustion / DoS.

---

### Likelihood Explanation

RBF is enabled by default (`min_rbf_rate = 1500 > min_fee_rate = 1000`): [12](#0-11) 

The attack requires only standard `send_transaction` RPC calls — no privileged role, no key, no majority hashpower. The attacker pays the fee for the high-fee child and its replacement, but the low-fee parent persists with inflated weights indefinitely. The attack is repeatable and cheap relative to the pool capacity it occupies.

---

### Proof of Concept

**Step-by-step (no code execution required — follows directly from the code paths above):**

1. Pool is near `max_tx_pool_size` (180 MB).
2. Attacker submits **tx_A** (very low fee, spending confirmed output X). tx_A enters the pool; `tx_A.descendants_fee = tx_A.fee`.
3. Attacker submits **tx_B** (high fee, spending tx_A's output **and** confirmed output Y). `add_entry` → `record_entry_descendants` → `update_ancestors_index_key(tx_B, Add)` → tx_A's `descendants_fee += tx_B.fee`, `descendants_size += tx_B.size`, `descendants_count = 2`. tx_A's `evict_key` now reflects a high `descendants_feerate`.
4. Attacker submits **tx_C** (fee > tx_B.fee, spending confirmed output Y only). tx_C conflicts with tx_B on input Y. RBF check passes. `process_rbf` calls `remove_entry_and_descendants(tx_B)`:
   - `remove_entry_links(tx_B)` removes tx_B from `self.links` and from tx_A's children-set.
   - `remove_entry(tx_B)` calls `update_ancestors_index_key(tx_B, Remove)` → `calc_ancestors(tx_B)` → `self.links.inner.get(tx_B)` returns `None` → empty set → **tx_A's `descendants_fee` is never decremented**.
5. tx_A remains in the pool with `descendants_fee = tx_A.fee + tx_B.fee` (stale). Its `EvictKey.fee_rate = descendants_feerate` (inflated). tx_A is sorted as if it were a high-fee transaction and is never evicted.
6. Repeat steps 2–5 for tx_A2, tx_A3, … to fill the pool with low-fee transactions that all appear high-fee. Legitimate high-fee transactions are rejected with `Reject::Full`. [13](#0-12) [14](#0-13) 

---

### Recommendation

In `remove_entry_and_descendants`, update the surviving ancestors' descendant weights **before** tearing down the links. Specifically, for the root entry `id`, call `update_ancestors_index_key(root_entry, EntryOp::Remove)` while `self.links` still contains the root's parent pointers, then proceed with `remove_entry_links` for the whole subtree. Alternatively, collect the root's ancestors before any link removal and apply `sub_descendant_weight` to each of them explicitly:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Fix: update ancestors' descendant weights BEFORE removing links
    if let Some(root_entry) = self.get(id).cloned() {
        self.update_ancestors_index_key(&root_entry, EntryOp::Remove);
    }

    // Now safe to remove all links (descendants' ancestor weights don't matter
    // since they are all being removed)
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

A corresponding unit test should assert that after `remove_entry_and_descendants(&tx2_id)`, `tx1.descendants_count == 1`, `tx1.descendants_fee == tx1.fee`, and `tx1.evict_key` reflects only tx1's own fee-rate.

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

**File:** tx-pool/src/component/entry.rs (L35-41)
```rust
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
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

**File:** tx-pool/src/pool.rs (L574-610)
```rust
    pub(crate) fn check_rbf(
        &self,
        snapshot: &Snapshot,
        entry: &TxEntry,
    ) -> Result<HashSet<ProposalShortId>, Reject> {
        assert!(self.enable_rbf());
        let tx_inputs: Vec<OutPoint> = entry.transaction().input_pts_iter().collect();
        let conflict_ids = self.pool_map.find_conflict_tx(entry.transaction());

        if conflict_ids.is_empty() {
            return Ok(HashSet::new());
        }

        let short_id = entry.proposal_short_id();

        // Rule #1, the node has enabled RBF, which is checked by caller
        let conflicts = conflict_ids
            .iter()
            .filter_map(|id| self.get_pool_entry(id))
            .collect::<Vec<_>>();
        assert!(conflicts.len() == conflict_ids.len());

        // Rule #2, new tx don't contain any new unconfirmed inputs
        let mut inputs = HashSet::new();
        for c in conflicts.iter() {
            inputs.extend(c.inner.transaction().input_pts_iter());
        }

        if tx_inputs
            .iter()
            .any(|pt| !inputs.contains(pt) && !snapshot.transaction_exists(&pt.tx_hash()))
        {
            return Err(Reject::RBFRejected(
                "new Tx contains unconfirmed inputs".to_string(),
            ));
        }

```

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
```

**File:** tx-pool/src/process.rs (L190-235)
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
    }
```
