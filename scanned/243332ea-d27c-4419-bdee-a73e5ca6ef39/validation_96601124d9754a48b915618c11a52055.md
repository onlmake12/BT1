### Title
Ancestors' Descendant-Weight State Not Updated in `remove_entry_and_descendants()` Leaves Pool Entries with Stale Eviction Keys — (File: `tx-pool/src/component/pool_map.rs`)

### Summary
`PoolMap::remove_entry_and_descendants()` pre-removes all link records before calling `remove_entry()` on each entry. Because `update_ancestors_index_key()` resolves ancestors through those same link records, ancestor entries that remain in the pool never have their `descendants_count / descendants_fee / descendants_size / descendants_cycles` decremented. The resulting inflated eviction keys cause those ancestors to be treated as more valuable than they are, preventing correct eviction and enabling a tx-pool DoS by any unprivileged transaction submitter.

### Finding Description

`remove_entry_and_descendants` first strips all link records for every entry being removed, then calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← all links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
``` [1](#0-0) 

Inside `remove_entry`, the call to `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors(...)`:

```rust
// tx-pool/src/component/pool_map.rs  lines 235-250
pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
    self.entries.remove_by_id(id).map(|entry| {
        self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);  // ← uses links
        self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
        ...
    })
}
``` [2](#0-1) 

```rust
// tx-pool/src/component/pool_map.rs  lines 432-444
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← empty: links already gone
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
``` [3](#0-2) 

Because the links are already gone when `update_ancestors_index_key` runs, `calc_ancestors` returns an empty set. Any ancestor of the removed subtree that **remains** in the pool never receives the `sub_descendant_weight` call, leaving its `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` permanently inflated.

By contrast, the single-entry path `remove_entry` (called directly, not through `remove_entry_and_descendants`) removes links **after** updating ancestor weights, so ancestors are correctly decremented in that path. [2](#0-1) 

The stale fields feed directly into `EvictKey`:

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
            descendants_count: entry.descendants_count,
        }
    }
}
``` [4](#0-3) 

`limit_size` evicts entries in ascending `EvictKey` order. An ancestor with an inflated `descendants_feerate` appears more valuable than it is and is skipped during eviction. [5](#0-4) 

### Impact Explanation

An ancestor entry that remains in the pool after `remove_entry_and_descendants` carries a permanently inflated eviction key. When the pool reaches its size limit (`max_tx_pool_size`), `limit_size` iterates entries by ascending evict key and skips the stale ancestor. Legitimate incoming transactions are rejected with `Reject::Full` while the stale ancestor occupies pool space it should have vacated. The pool's `total_tx_size` accounting is correct (it is updated per-entry in `update_stat_for_remove_tx`), but the per-entry `descendants_*` fields used for eviction priority are wrong, causing systematic misordering of eviction candidates. [6](#0-5) 

### Likelihood Explanation

The trigger is `resolve_conflict`, which is called on every transaction submission that spends an already-spent input:

```rust
// tx-pool/src/pool.rs  lines 253-267
fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
    ...
    for (entry, reject) in self.pool_map.resolve_conflict(tx) { ... }
}
``` [7](#0-6) 

`resolve_conflict` calls `remove_entry_and_descendants` for every conflicting entry. Any unprivileged RPC caller (`send_transaction`) can trigger this path by submitting a double-spend. No special privilege, key, or majority hash power is required. [8](#0-7) 

### Recommendation

Move the ancestor-weight update **before** link removal in `remove_entry_and_descendants`, or explicitly walk the ancestors of the root entry and call `sub_descendant_weight` for each removed entry before tearing down links. The single-entry `remove_entry` path already does this correctly and can serve as the reference implementation.

### Proof of Concept

1. Submit `tx0` (low fee, spends a confirmed cell).
2. Submit `tx1` (child of `tx0`, very high fee).
3. Submit `tx2`, `tx3` (descendants of `tx1`, high fee). At this point `tx0.descendants_count = 4`, `tx0.descendants_fee` is large.
4. Submit `tx1'` that spends the same input as `tx1` (double-spend / RBF). `resolve_conflict` fires, calling `remove_entry_and_descendants(tx1_id)`.
5. `tx1`, `tx2`, `tx3` are removed. `tx0` remains with `descendants_count = 4` (should be `1`) and inflated `descendants_fee`.
6. Fill the pool to `max_tx_pool_size` with medium-fee transactions. `limit_size` iterates by evict key; `tx0` is skipped because its inflated `descendants_feerate` makes it appear more valuable than the medium-fee entries.
7. New legitimate transactions are rejected with `Reject::Full` while `tx0` (a low-fee transaction) persists in the pool indefinitely. [1](#0-0) [9](#0-8)

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

**File:** tx-pool/src/pool.rs (L253-267)
```rust
    fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
        let short_id = tx.proposal_short_id();
        if let Some(_entry) = self.pool_map.remove_entry(&short_id) {
            debug!("remove_committed_tx for {}", tx.hash());
        }
        {
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
            }
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
