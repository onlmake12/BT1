### Title
Stale Descendant Weight Tracking After `remove_entry_and_descendants` Causes Incorrect Pool Eviction Ordering — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all parent/child links before calling `remove_entry` on each entry. This prevents `update_ancestors_index_key` from decrementing the `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` of ancestors that **remain** in the pool. Those ancestors retain permanently inflated descendant-weight metrics, causing their `EvictKey` to be overstated. When the pool is full, these ancestors survive eviction longer than their true fee rate warrants, and legitimate high-fee transactions from other users may be evicted in their place.

---

### Finding Description

`PoolMap` tracks per-entry descendant weight through four fields on `TxEntry`:

```
descendants_fee, descendants_size, descendants_cycles, descendants_count
``` [1](#0-0) 

These fields are updated incrementally: `add_descendant_weight` / `sub_descendant_weight` are called by `update_ancestors_index_key` whenever a child is added or removed. [2](#0-1) 

The eviction key is derived directly from these fields: [3](#0-2) 

`remove_entry_and_descendants` is the bulk-removal path used by RBF, conflict resolution, and header-dep invalidation. It first strips all links for every entry to be removed, **then** calls `remove_entry` on each: [4](#0-3) 

Inside `remove_entry`, `update_ancestors_index_key` is called to decrement the descendant weights of surviving ancestors: [5](#0-4) 

However, `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, which traverses the link graph: [2](#0-1) 

Because `remove_entry_links` was already called for every entry in the batch (including the root), the link graph no longer contains any path from the removed entry back to its surviving ancestors. `calc_ancestors` returns an empty set, and **no ancestor's descendant weight is ever decremented**.

The comment in the code acknowledges this is intentional to avoid double-updating descendants, but it silently skips the necessary update to surviving ancestors: [6](#0-5) 

**Concrete scenario with RBF enabled (default config):**

1. Attacker submits tx **A** (low fee, e.g. 100 shannons).
2. Attacker submits tx **B** (child of A, high fee, e.g. 5 000 000 shannons) and tx **C** (child of B, high fee).
3. A's `descendants_fee` = 100 + 5 000 000 + C_fee. A's `EvictKey.fee_rate` is high.
4. Attacker submits tx **B′** that conflicts with B (RBF). `process_rbf` calls `remove_entry_and_descendants(B)`, removing B and C.
5. Due to the bug, A's `descendants_fee` is **never decremented**. It still reflects B and C's fees.
6. B′ is inserted as a new child of A. `record_entry_descendants` calls `update_ancestors_index_key(B′, Add)`, adding B′'s fee on top of the already-stale value.
7. A's `descendants_fee` = 100 + 5 000 000 + C_fee + B′_fee (should be 100 + B′_fee).
8. When the pool fills up and `limit_size` runs, A's inflated `EvictKey` causes it to survive eviction while legitimate high-fee transactions from other users are dropped. [7](#0-6) 

The same staleness occurs whenever `remove_entry_and_descendants` is called with a non-root entry that has surviving ancestors: conflict resolution on block commit, header-dep invalidation, and the ancestor-count eviction path inside `check_and_record_ancestors`. [8](#0-7) 

---

### Impact Explanation

- **Incorrect eviction ordering**: Ancestors of removed subtrees have permanently inflated `EvictKey.fee_rate`. They are evicted later than their true fee rate warrants.
- **Pool admission DoS**: When the pool is at capacity, legitimate high-fee transactions submitted by other users are evicted in preference to the attacker's low-fee ancestor, because the attacker's entry appears more valuable than it is.
- **Compounding on repeated RBF**: Each RBF cycle that removes children adds another layer of inflation to the surviving ancestor's descendant weight, worsening the divergence over time.
- **Stale RPC data**: `get_pool_tx_detail_info` returns stale `descendants_size` / `descendants_cycles` for surviving ancestors.

**Impact: Medium** — affects pool eviction fairness and can be used to keep low-fee transactions alive at the expense of other users' transactions.

---

### Likelihood Explanation

- RBF is enabled by default in the production config (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`).
- Any unprivileged tx-pool submitter can craft the required transaction chain.
- The bug is triggered on every `remove_entry_and_descendants` call where the removed root has surviving ancestors — a common occurrence during RBF and conflict resolution.
- No special privileges, keys, or majority hash power are required.

**Likelihood: Medium** — requires a near-full pool for the eviction impact to materialize, but the tracking corruption itself occurs unconditionally.

---

### Recommendation

In `remove_entry_and_descendants`, before stripping links, collect the set of surviving ancestors of the root entry and decrement their descendant weights explicitly:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Collect surviving ancestors of the root BEFORE links are removed
    let surviving_ancestors = self.links.calc_ancestors(id);

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Decrement descendant weights for each removed entry on surviving ancestors
    for removed_entry in &removed {
        for anc_id in &surviving_ancestors {
            self.entries.modify_by_id(anc_id, |e| {
                e.inner.sub_descendant_weight(removed_entry);
                e.evict_key = e.inner.as_evict_key();
            });
        }
    }

    removed
}
```

Alternatively, restructure `remove_entry` to accept an explicit ancestor set so that the link graph does not need to be intact at call time.

---

### Proof of Concept

**Setup** (pool with RBF enabled, `min_rbf_rate > min_fee_rate`):

1. Submit tx **A** (fee = 100 shannons, size = S).
2. Submit tx **B** spending A's output (fee = 5 000 000 shannons).
3. Submit tx **C** spending B's output (fee = 5 000 000 shannons).
   → A's `descendants_fee` = 100 + 5 000 000 + 5 000 000 = 10 000 100.
4. Submit tx **B′** conflicting with B, fee > B's fee + RBF surcharge.
   → `process_rbf` calls `remove_entry_and_descendants(B)`.
   → B and C are removed. A's `descendants_fee` remains 10 000 100 (bug).
5. B′ is inserted as child of A.
   → `update_ancestors_index_key(B′, Add)` adds B′'s fee to A's `descendants_fee`.
   → A's `descendants_fee` = 10 000 100 + B′_fee (should be 100 + B′_fee).
6. Fill the pool with many medium-fee transactions until `total_tx_size > max_tx_pool_size`.
7. Observe via `tx_pool_info` / `get_pool_tx_detail_info` that A survives eviction while medium-fee transactions from other users are dropped, despite A's true fee being only 100 shannons. [9](#0-8) [4](#0-3) [10](#0-9)

### Citations

**File:** tx-pool/src/component/entry.rs (L34-41)
```rust
    /// descendants txs fee
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
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
