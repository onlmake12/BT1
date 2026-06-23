### Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

When `remove_entry_and_descendants` removes a transaction and all its descendants from the tx-pool, it first strips all link entries for every removed transaction before calling `remove_entry` on each. Because `remove_entry` relies on the live link graph to discover ancestors and update their `descendants_*` accounting fields and derived `evict_key`, the pre-removal link teardown causes those ancestor updates to be silently skipped. Ancestors that remain in the pool are left with inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, and therefore a stale, artificially high `evict_key`. This is the direct CKB analog of the Strata `Accounting::accrueFee` bug: a state-modifying operation updates the primary accounting fields but omits the recalculation of the derived value (`evict_key` / APR target) that downstream logic depends on.

---

### Finding Description

`PoolMap::remove_entry_and_descendants` collects the root id and all descendant ids, then calls `remove_entry_links` on every one of them before iterating to call `remove_entry`: [1](#0-0) 

`remove_entry_links` removes the target from its parents' children sets **and** deletes the target's own link record from `self.links`: [2](#0-1) 

After that teardown, `remove_entry` is called for each removed id. Inside `remove_entry`, `update_ancestors_index_key` is invoked to propagate the removal upward to surviving ancestors: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())` to discover which pool entries to update: [4](#0-3) 

Because `remove_entry_links` already deleted the removed transaction's link record, `calc_ancestors` returns an **empty set**. The `for anc_id in &ancestors` loop body never executes. Surviving ancestors never receive `sub_descendant_weight`, and their `evict_key` is never recomputed. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields remain inflated.

The `evict_key` is the sole ordering key used by `next_evict_entry` to select which transaction to drop when the pool is full: [5](#0-4) 

`EvictKey` is derived from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`: [6](#0-5) 

A stale, inflated `evict_key` makes the ancestor appear to have a higher effective fee rate than it actually does, so it is ranked lower in the eviction order (i.e., it is less likely to be evicted).

`remove_entry_and_descendants` is called from every conflict-resolution path: [7](#0-6) 

---

### Impact Explanation

When the tx-pool reaches its size limit (`limit_size`), it iterates `next_evict_entry` to drop the lowest-priority transaction. Because an ancestor's `evict_key` is stale and inflated, it is skipped in favour of a transaction that genuinely has a lower fee rate. The result is:

- A low-fee-rate ancestor transaction survives eviction rounds it should not survive.
- Legitimate higher-fee-rate transactions submitted by honest users are rejected with `Reject::Full` instead.
- The `evict_key` is also used in `check_and_record_ancestors` to select which cell-dep-conflicting transactions to evict when ancestor-count limits are hit; stale keys cause the wrong transactions to be evicted there as well. [8](#0-7) 

---

### Likelihood Explanation

The trigger is fully unprivileged and requires only standard RPC access (`send_transaction`):

1. Attacker submits parent transaction **P** with a low fee rate.
2. Attacker submits child transaction **C** spending an output of P, with a high fee rate. `record_entry_descendants` → `update_ancestors_index_key` correctly inflates P's `descendants_*` fields and `evict_key`.
3. Attacker submits a second transaction **C′** that spends the same input as C (double-spend / RBF). `resolve_conflict` calls `remove_entry_and_descendants(C)`. P's `descendants_*` fields and `evict_key` are **not** deflated.
4. P now permanently holds an inflated `evict_key` for the remainder of its time in the pool.
5. Repeating steps 2–3 with fresh children keeps P's `evict_key` perpetually stale.

No privileged role, no majority hash power, and no social engineering is required.

---

### Recommendation

Before stripping link records in `remove_entry_and_descendants`, collect the surviving ancestors of the root transaction and, after all removals are complete, call `sub_descendant_weight` and recompute `evict_key` for each of them. Concretely:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Snapshot surviving ancestors BEFORE tearing down links.
    let surviving_ancestors: HashSet<ProposalShortId> = self
        .links
        .calc_ancestors(id)
        .into_iter()
        .filter(|a| !removed_ids.contains(a))
        .collect();

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Update surviving ancestors' descendant accounting.
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

---

### Proof of Concept

```
State: pool is near its size limit.

1. submit_transaction(P)   // low fee rate, e.g. 1 shannon/byte
   → P.descendants_fee = P.fee
   → P.evict_key reflects fee_rate(P)

2. submit_transaction(C)   // high fee rate, spends P output
   → update_ancestors_index_key(C, Add) fires
   → P.descendants_fee += C.fee   (inflated)
   → P.evict_key = max(descendants_feerate, feerate)  ← now high

3. submit_transaction(C')  // conflicts with C (same input)
   → resolve_conflict → remove_entry_and_descendants(C)
   → remove_entry_links called for C before remove_entry
   → update_ancestors_index_key(C, Remove) finds calc_ancestors(C) == ∅
   → P.descendants_fee is NOT decremented  (still inflated)
   → P.evict_key is NOT recomputed         (still high)

4. Pool fills up; limit_size calls next_evict_entry.
   → P is skipped because its evict_key shows a high fee rate.
   → A legitimate transaction Q with a genuinely higher fee rate
     than P is evicted instead.
   → Q's submitter receives Reject::Full.
``` [9](#0-8) [1](#0-0) [6](#0-5)

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

**File:** tx-pool/src/component/pool_map.rs (L305-331)
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
