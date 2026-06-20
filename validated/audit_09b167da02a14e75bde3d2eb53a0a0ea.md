### Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Statistics — (File: `tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records before calling `remove_entry` on each evicted transaction. Because `update_ancestors_index_key` resolves ancestors through those same link records, it finds an empty ancestor set for every removed entry and never decrements the descendant-weight fields (`descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`) of transactions that **remain** in the pool. Those surviving ancestors carry permanently inflated descendant statistics and a stale `evict_key`, corrupting the pool's eviction ordering for the lifetime of those entries.

---

### Finding Description

`PoolMap` maintains per-entry descendant accounting fields and a derived `evict_key` that drives eviction priority. When a batch removal is requested, the code first strips all link records for every entry in the batch, then calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs  remove_entry_and_descendants
for id in &removed_ids {
    self.remove_entry_links(id);          // ← links torn down for ALL entries
}
removed_ids
    .iter()
    .filter_map(|id| self.remove_entry(id))
    .collect()
```

Inside `remove_entry`, the first thing called is `update_ancestors_index_key`:

```rust
// remove_entry
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

That function resolves ancestors through `self.links`:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            e.inner.sub_descendant_weight(child);   // ← never reached
            e.evict_key = e.inner.as_evict_key();   // ← never reached
        });
    }
}
```

Because `remove_entry_links` was already called for every entry in the batch, `calc_ancestors` returns an empty set for each removed entry. Ancestors that **remain** in the pool are never visited; their `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and `evict_key` are never decremented. [1](#0-0) [2](#0-1) [3](#0-2) 

The comment acknowledges only half the intent:

```
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
```

The goal is to avoid updating the ancestor weights of entries that are themselves being removed (correct). The unintended side-effect is that `update_ancestors_index_key` — which updates the **descendant** weights of entries that are **not** being removed — is also silenced. [4](#0-3) 

---

### Impact Explanation

Every code path that calls `remove_entry_and_descendants` on a transaction that has a surviving ancestor is affected:

| Caller | Trigger |
|---|---|
| `resolve_conflict` | committed tx consumes an in-pool input |
| `resolve_conflict_header_dep` | reorg detaches a header dep |
| `limit_size` | pool exceeds `max_tx_pool_size` |
| `check_and_record_ancestors` | ancestor-count eviction during RBF |
| `process_rbf` | RBF replacement |
| `remove_by_detached_proposal` | proposal window expiry | [5](#0-4) [6](#0-5) 

After any such removal, surviving ancestors carry stale `evict_key` values. `next_evict_entry` iterates by `evict_key`, so the pool evicts transactions in the wrong order when it is full. A transaction that should be the first candidate for eviction (low fee, no real descendants) may be skipped because its `evict_key` still encodes phantom descendants, while a higher-value transaction is evicted instead. [7](#0-6) [8](#0-7) 

Additionally, `tx_pool_info` and `get_pool_tx_detail_info` RPC responses will report incorrect descendant counts for affected entries for as long as they remain in the pool. [9](#0-8) 

---

### Likelihood Explanation

The bug fires on every call to `remove_entry_and_descendants` where the removed entry has at least one ancestor still in the pool. This is the common case for:

- **RBF**: a new transaction replaces an existing one; the replaced transaction's parent chain often remains.
- **Block commit**: `remove_committed_tx` calls `resolve_conflict` which calls `remove_entry_and_descendants` for every in-pool transaction that spent the same input as the committed one.
- **Pool eviction**: `limit_size` repeatedly calls `remove_entry_and_descendants`; each call may leave ancestors with stale stats, corrupting the next iteration's eviction choice.

Any unprivileged peer or RPC caller that submits a chain of two or more transactions and then triggers a conflict (e.g., via a second `send_transaction` spending the same cell) will exercise this path. [10](#0-9) [11](#0-10) 

---

### Recommendation

Move the ancestor-weight update **before** the link teardown, or perform it separately for entries that are not in the removal batch. Concretely, in `remove_entry_and_descendants`, after computing `removed_ids` and before calling `remove_entry_links`, iterate over each entry's ancestors that are **not** in `removed_ids` and call `sub_descendant_weight` + `evict_key` refresh on them directly. Only then tear down the links.

Alternatively, restructure `remove_entry` so that `update_ancestors_index_key` accepts an explicit ancestor set rather than resolving it from `self.links`, allowing the caller to pass the correct set even after links have been removed. [12](#0-11) [13](#0-12) 

---

### Proof of Concept

1. Submit `tx1` (pending, low fee-rate).
2. Submit `tx2` spending `tx1`'s output (pending).
3. Submit `tx3` spending `tx2`'s output (pending). Now `tx1.descendants_count == 2`.
4. Submit `tx4` that double-spends `tx2`'s input (RBF or conflict). `resolve_conflict` → `remove_entry_and_descendants(tx2)` removes `tx2` and `tx3`.
5. `tx1` remains. Inspect via `get_pool_tx_detail_info`: `descendants_count` still reports `2` instead of `0`; `evict_key` is stale.
6. Fill the pool to trigger `limit_size`. `next_evict_entry` iterates by `evict_key`; `tx1` is skipped (phantom descendants inflate its key) even though it is now a leaf with no real descendants, causing a higher-value transaction to be evicted instead. [14](#0-13) [15](#0-14)

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

**File:** tx-pool/src/component/pool_map.rs (L447-460)
```rust
    fn update_descendants_index_key(&mut self, parent: &TxEntry, op: EntryOp) {
        let descendants: HashSet<ProposalShortId> =
            self.links.calc_descendants(&parent.proposal_short_id());
        for desc_id in &descendants {
            // update child score
            self.entries.modify_by_id(desc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_ancestor_weight(parent),
                    EntryOp::Add => e.inner.add_ancestor_weight(parent),
                };
                e.score = e.inner.as_score_key();
            });
        }
    }
```

**File:** tx-pool/src/component/pool_map.rs (L585-640)
```rust
    // this is not an issue currently, because RBF have a rule that not allow any unknown inputs except
    // the conflicted inputs, so the new transaction can not be in a long transaction chain.
    // but it's still safer to report an error before any writing kind of operation.
    fn check_and_record_ancestors(
        &mut self,
        entry: &mut TxEntry,
    ) -> Result<HashSet<TxEntry>, Reject> {
        let tx = entry.transaction();
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }

        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();

            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
        } else {
            return Err(Reject::ExceededMaximumAncestorsCount);
        }

        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);

        self._record_ancestors(entry, ancestors, parents);
        Ok(evicted)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L668-696)
```rust
    fn track_entry_statics(&mut self, remove: Option<Status>, add: Option<Status>) {
        match remove {
            Some(Status::Pending) => self.pending_count -= 1,
            Some(Status::Gap) => self.gap_count -= 1,
            Some(Status::Proposed) => self.proposed_count -= 1,
            _ => {}
        }
        match add {
            Some(Status::Pending) => self.pending_count += 1,
            Some(Status::Gap) => self.gap_count += 1,
            Some(Status::Proposed) => self.proposed_count += 1,
            _ => {}
        }
        assert_eq!(
            self.pending_count + self.gap_count + self.proposed_count,
            self.entries.len()
        );
        if let Some(metrics) = ckb_metrics::handle() {
            metrics
                .ckb_tx_pool_entry
                .pending
                .set(self.pending_count as i64);
            metrics.ckb_tx_pool_entry.gap.set(self.gap_count as i64);
            metrics
                .ckb_tx_pool_entry
                .proposed
                .set(self.proposed_count as i64);
        }
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

**File:** tx-pool/src/service.rs (L1086-1097)
```rust
            pending_size: tx_pool.pool_map.pending_size(),
            proposed_size: tx_pool.pool_map.proposed_size(),
            orphan_size: orphan.len(),
            total_tx_size: tx_pool.pool_map.total_tx_size,
            total_tx_cycles: tx_pool.pool_map.total_tx_cycles,
            min_fee_rate: self.tx_pool_config.min_fee_rate,
            min_rbf_rate: self.tx_pool_config.min_rbf_rate,
            last_txs_updated_at: tx_pool.pool_map.get_max_update_time(),
            tx_size_limit: TRANSACTION_SIZE_LIMIT,
            max_tx_pool_size: self.tx_pool_config.max_tx_pool_size as u64,
            verify_queue_size: verify_queue.len(),
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
