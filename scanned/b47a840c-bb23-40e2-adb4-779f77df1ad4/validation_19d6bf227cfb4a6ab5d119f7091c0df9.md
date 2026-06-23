### Title
Stale `descendants_*` Accounting in `PoolMap::remove_entry_and_descendants` Allows Attacker to Inflate Ancestor Eviction Priority — (`tx-pool/src/component/pool_map.rs`)

### Summary

When `PoolMap::remove_entry_and_descendants` removes a transaction and all its descendants, it first clears all link records for every removed entry before calling `remove_entry` on each. Because `update_ancestors_index_key` relies on those link records to find which still-live ancestors need their `descendants_*` fields decremented, the link-clearing step silently prevents any ancestor update. Ancestors of the removed root transaction retain inflated `descendants_size`, `descendants_cycles`, `descendants_fee`, and `descendants_count` values. An unprivileged tx-pool submitter can exploit this via RBF to repeatedly inflate an ancestor's apparent descendant weight, making a low-fee transaction appear more valuable than it is and preventing it from being evicted when the pool is full.

### Finding Description

`PoolMap::remove_entry_and_descendants` is the code path used whenever a transaction and its entire subtree must be expelled from the pool — during RBF replacement (`process_rbf`), conflict resolution on block commit (`resolve_conflict`), and header-dep invalidation (`resolve_conflict_header_dep`). [1](#0-0) 

The implementation first removes link records for every entry in the subtree, then calls `remove_entry` on each:

```
for id in &removed_ids {
    self.remove_entry_links(id);   // ← links cleared for ALL removed entries
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

Inside `remove_entry`, `update_ancestors_index_key` is called to decrement `descendants_*` on every still-live ancestor: [2](#0-1) 

That function calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because the links for the removed entry were already cleared in the loop above, `calc_ancestors` returns an empty set. No ancestor receives a `sub_descendant_weight` call. The comment in the source acknowledges the intent to skip descendant-side updates, but the side-effect of also skipping ancestor-side updates is not addressed: [3](#0-2) 

By contrast, when `remove_entry` is called directly (not via `remove_entry_and_descendants`), `remove_entry_links` is called *after* `update_ancestors_index_key`, so ancestors are correctly updated: [4](#0-3) 

The `descendants_*` fields are used to compute each entry's `EvictKey`: [5](#0-4) 

`EvictKey.fee_rate` is `max(own_feerate, descendants_feerate)`. An inflated `descendants_fee` with a correspondingly inflated `descendants_size`/`descendants_cycles` can raise `descendants_feerate` above the entry's true fee rate, making the entry appear more valuable and less likely to be evicted.

Each RBF replacement of a descendant compounds the inflation: the old descendant's contribution is never subtracted, and the new descendant's contribution is added on top. [6](#0-5) 

### Impact Explanation

- **Incorrect eviction priority**: Ancestors of RBF-replaced transactions retain inflated `descendants_*`, causing `limit_size` to spare them when the pool is full.
- **Pool space exhaustion**: A low-fee transaction can be kept alive indefinitely by repeatedly submitting and RBF-replacing high-fee descendants, each cycle adding to the ancestor's apparent descendant weight.
- **Legitimate transaction rejection**: When the pool is full and eviction selects victims by lowest `EvictKey`, honest high-fee transactions may be rejected while the attacker's low-fee transaction survives with an artificially elevated priority. [7](#0-6) 

### Likelihood Explanation

RBF is enabled by default in the mainnet configuration (`min_rbf_rate = 1_500 > min_fee_rate = 1_000`): [8](#0-7) 

Any unprivileged tx-pool submitter can trigger this path by submitting a parent transaction, then repeatedly submitting and RBF-replacing a child transaction. The attacker pays the RBF fee premium per replacement, but the cost is bounded and predictable, while the benefit (keeping a low-fee transaction alive in a full pool) can persist indefinitely.

### Recommendation

Before clearing link records in `remove_entry_and_descendants`, update the ancestors of the root entry being removed. Specifically, call `update_ancestors_index_key(&root_entry, EntryOp::Remove)` while the root entry's links are still intact, so that every still-live ancestor receives a correct `sub_descendant_weight` call. Alternatively, restructure `remove_entry_and_descendants` to update ancestor accounting for the root entry before any link removal occurs.

### Proof of Concept

1. Submit `tx_A` (low fee rate, just above `min_fee_rate`) to the pool.
2. Submit `tx_B` (child of `tx_A`, very high fee rate) to the pool.
   - `tx_A.descendants_fee` now includes `tx_B.fee`; `tx_A.EvictKey.fee_rate` = `tx_B`'s high rate.
3. Submit `tx_B'` (same inputs as `tx_B`, higher fee, satisfying RBF rules) to trigger `process_rbf`.
   - `remove_entry_and_descendants(tx_B)` is called.
   - Links for `tx_B` are cleared before `remove_entry(tx_B)` runs.
   - `update_ancestors_index_key(tx_B, Remove)` finds no ancestors (links gone); `tx_A.descendants_*` is **not decremented**.
   - `tx_B'` is then inserted; `update_ancestors_index_key(tx_B', Add)` correctly increments `tx_A.descendants_*`.
   - `tx_A.descendants_fee` now equals `tx_A.fee + tx_B.fee + tx_B'.fee` — doubly inflated.
4. Repeat step 3 N times. After N replacements, `tx_A.descendants_fee` ≈ `tx_A.fee + N × tx_B_fee + tx_B'_fee`, making `tx_A` effectively immune to eviction by `limit_size`. [9](#0-8) [1](#0-0)

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
