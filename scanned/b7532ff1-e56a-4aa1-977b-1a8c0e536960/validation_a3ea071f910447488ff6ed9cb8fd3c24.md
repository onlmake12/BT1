### Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Keys - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records for every entry in the removal set **before** calling `remove_entry` on each one. Because `remove_entry` relies on those same links to locate and update ancestor entries' descendant-weight fields (`descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`), the update is silently skipped. Ancestor transactions that remain in the pool are left with permanently inflated descendant-weight accounting, corrupting their `evict_key` and therefore the pool's eviction ordering.

---

### Finding Description

`PoolMap` maintains per-entry descendant-weight fields that are used to compute each entry's `EvictKey`. These fields must be decremented whenever a descendant is removed.

The normal single-entry removal path (`remove_entry`) does this correctly: [1](#0-0) 

```
remove_entry(id):
  1. remove from entries map
  2. update_ancestors_index_key(entry, Remove)   ← decrements ancestors' descendant weights
  3. update_descendants_index_key(entry, Remove)
  4. remove_entry_edges
  5. remove_entry_links(id)                      ← links removed LAST
  6. track_entry_statics / update_stat_for_remove_tx
```

The bulk-removal path (`remove_entry_and_descendants`) breaks this ordering: [2](#0-1) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← ALL links erased FIRST
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

When `remove_entry` is subsequently called for each entry, `update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

Because the entry's own link record was already erased in the pre-removal loop, `calc_ancestors` returns an empty set. The loop body that calls `e.inner.sub_descendant_weight(child)` and refreshes `e.evict_key` is never executed for any ancestor that lies **outside** the removed set.

The descendant-weight fields on those surviving ancestors are never decremented: [4](#0-3) 

The global pool statistics (`total_tx_size`, `total_tx_cycles`) are still updated correctly via `update_stat_for_remove_tx`, so pool-size enforcement is unaffected. Only the per-entry descendant-weight fields are stale.

---

### Impact Explanation

Each entry's `evict_key` is recomputed from its descendant-weight fields inside `update_ancestors_index_key`. With stale (inflated) descendant weights, the `evict_key` of surviving ancestor entries is wrong. `next_evict_entry`, called by `limit_size`, uses this key to choose which transaction to drop when the pool is full: [5](#0-4) 

Consequences:
- A low-fee ancestor transaction whose child was removed via conflict resolution retains an inflated apparent descendant fee, making it appear more valuable than it is and causing it to survive eviction rounds it should lose.
- Legitimate high-fee transactions may be evicted in its place.
- The stale state is permanent for the lifetime of the ancestor entry in the pool; it is never corrected unless the ancestor itself is removed.

---

### Likelihood Explanation

`remove_entry_and_descendants` is invoked on every conflict-resolution event: [6](#0-5) 

Any unprivileged peer or RPC caller can trigger this by:
1. Submitting a parent transaction (Tx A) and a child transaction (Tx B spending A's output).
2. Submitting a second transaction (Tx C) that spends the same output as Tx B (double-spend / RBF attempt that fails or succeeds).

Step 2 causes `resolve_conflict` → `remove_entry_and_descendants(B)`. Tx A remains in the pool with inflated `descendants_*` fields. This is a standard, low-cost operation requiring no special privilege.

---

### Recommendation

Move the link-removal step to **after** the ancestor-weight update, or pass the set of entries being removed into `remove_entry` so it can skip updating entries that are themselves being removed. A minimal fix for `remove_entry_and_descendants`:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update ancestor weights BEFORE erasing links, so surviving ancestors
    // get their descendant-weight fields correctly decremented.
    let results: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    results
}
```

The original comment's concern (avoiding redundant `update_descendants_index_key` calls for entries that are also being removed) can be addressed by checking membership in `removed_ids` inside `update_descendants_index_key`, rather than by pre-erasing all links.

---

### Proof of Concept

1. Submit Tx A (parent, low fee-rate) via `send_transaction`.
2. Submit Tx B (child of A, any fee-rate) via `send_transaction`.
3. Submit Tx C (spends the same input as B) via `send_transaction` — triggers `resolve_conflict` → `remove_entry_and_descendants(B)`.
4. Query `tx_pool_info` and observe `total_tx_size` / `total_tx_cycles` are correct (B is gone).
5. Inspect Tx A's internal pool entry: `descendants_count` is still 1, `descendants_size` and `descendants_fee` still include B's contribution — the stale values are never zeroed.
6. Fill the pool to capacity with higher-fee transactions. Observe that Tx A survives eviction rounds it should lose, because its `evict_key` is computed from the inflated descendant fee. [2](#0-1) [3](#0-2) [7](#0-6)

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

**File:** tx-pool/src/pool.rs (L253-268)
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
