### Title
Stale Descendant-Weight State in `remove_entry_and_descendants` Corrupts Eviction-Key Accounting — (File: tx-pool/src/component/pool_map.rs)

---

### Summary

`PoolMap::remove_entry_and_descendants` strips all link records for every entry being removed **before** calling `remove_entry` on each one. Because `remove_entry` relies on the live link graph to locate and update the surviving ancestors' `descendants_fee / descendants_size / descendants_cycles / descendants_count` fields, those fields are never decremented. Ancestor entries that remain in the pool permanently carry inflated descendant-weight statistics, corrupting the `EvictKey` that governs which transactions are dropped when the pool is full.

This is a direct structural analog of the reported `fundContract.sol` bug: a cumulative accounting field (`managementFee` / `descendants_fee`) is not reduced when a partial removal occurs, so every subsequent operation that reads that field operates on a stale, over-stated value.

---

### Finding Description

**Root cause — `remove_entry_and_descendants`** [1](#0-0) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips ALL link records first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

The comment explains the intent: pre-removing links prevents `update_descendants_index_key` from being called inside `remove_entry` (correct — descendants are being removed anyway). However, the same pre-removal also silences `update_ancestors_index_key`, which is supposed to walk **upward** and decrement the surviving ancestors' `descendants_*` counters.

**How `remove_entry` relies on the link graph** [2](#0-1) 

```rust
pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
    self.entries.remove_by_id(id).map(|entry| {
        self.update_ancestors_index_key(&entry.inner, EntryOp::Remove); // ← needs live links
        self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
        ...
    })
}
```

**`update_ancestors_index_key` finds nothing because links are already gone** [3](#0-2) 

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id()); // returns ∅ — link entry removed
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child), // never reached
                ...
            };
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

`sub_descendant_weight` — the function that should have been called on every surviving ancestor — is never invoked: [4](#0-3) 

**Stale fields corrupt the `EvictKey`**

The `EvictKey` is computed directly from the stale `descendants_fee / descendants_size / descendants_cycles / descendants_count`: [5](#0-4) 

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate), // inflated if descendants_fee is stale
            ...
        }
    }
}
```

After the removal, the ancestor's `EvictKey` still reflects the fee rate of the removed descendants. The pool's eviction loop (`limit_size`) picks the entry with the **lowest** `EvictKey` to drop; an ancestor with an artificially high key is skipped even when its own fee rate is low. [6](#0-5) 

---

### Impact Explanation

A surviving ancestor transaction carries permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`. Its `EvictKey` overstates its effective fee rate. When the pool is full:

- The ancestor is ranked as if it were a high-fee transaction and is skipped during eviction.
- Legitimate high-fee transactions submitted by other users may be evicted in its place.
- The ancestor can remain in the pool indefinitely, occupying pool capacity and degrading throughput for honest users.

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
