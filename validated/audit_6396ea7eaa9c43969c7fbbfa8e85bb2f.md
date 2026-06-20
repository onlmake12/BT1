### Title
Stale `evict_key` on Pool Ancestors After Batch Removal Disrupts Eviction Ordering — (`tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries for every transaction in the batch **before** calling `remove_entry` on each one. Because `remove_entry` derives the set of ancestors to update from those same links, the pre-removal causes `update_ancestors_index_key` to find an empty ancestor set and skip updating the `evict_key` of any ancestor that remains in the pool. The result is a permanently stale `evict_key` on surviving ancestors, corrupting the eviction-priority ordering of the pool.

---

### Finding Description

`remove_entry_and_descendants` is the function used to remove a transaction and all of its descendants from the pool in one shot (called from `resolve_conflict`, `resolve_conflict_header_dep`, `limit_size`, `remove_by_detached_proposal`, and `remove_tx`). [1](#0-0) 

The function first strips every link for every entry in the batch, then calls `remove_entry` for each:

```
// Step 1 – pre-remove ALL links for tx1, tx2, tx3
for id in &removed_ids {
    self.remove_entry_links(id);   // ← tx0's child-list loses tx1 here
}

// Step 2 – remove each entry
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

Inside `remove_entry`, the two score-update helpers are called: [2](#0-1) 

`update_ancestors_index_key` walks **tx1's own link entry** to find its ancestors: [3](#0-2) 

Because `remove_entry_links(tx1)` was already called in Step 1, `self.links.calc_ancestors(&tx1.proposal_short_id())` returns an **empty set**. The loop body never executes, so `tx0`'s `evict_key` (which encodes `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles`) is **never decremented**.

By contrast, the single-entry path `remove_entry` calls `remove_entry_links` **after** the score updates, so ancestors are still reachable and correctly updated: [4](#0-3) 

The `EvictKey` struct that is left stale: [5](#0-4) 

---

### Impact Explanation

The `evict_key` index is the sole ordering used by `next_evict_entry` to decide which transaction to drop when the pool exceeds its size limit: [6](#0-5) 

It is also used in `check_and_record_ancestors` to rank and evict `cell_ref_parents` when an incoming transaction would exceed `max_ancestors_count`: [7](#0-6) 

After the batch removal, `tx0`'s `evict_key` still reflects the fee-rate and descendant count of the removed children. Concretely:

- If the removed descendants had **high fees**, `tx0`'s `descendants_feerate` is inflated → `tx0` appears more valuable than it is → it is **not evicted** when it should be, and some other legitimate transaction is dropped instead.
- If the removed descendants had **low fees**, `tx0`'s `descendants_feerate` is deflated → `tx0` is **evicted prematurely**, even though its true standalone fee-rate is acceptable.
- `descendants_count` remains at the pre-removal value, further corrupting the ordering key.

The stale state persists until `tx0` itself is removed or the pool is restarted. Unlike the Badger finding where the invariant self-healed on the next redemption, here the stale `evict_key` is never recalculated unless `tx0` is explicitly touched again.

---

### Likelihood Explanation

The trigger is `resolve_conflict`, which is called every time a submitted transaction spends an input already consumed by a pool transaction: [8](#0-7) 

Any unprivileged actor — an RPC caller (`send_transaction`), a relay peer, or an RBF submitter — can reach this path by submitting a transaction that double-spends an input of a pool transaction that itself has a parent still in the pool. This is a routine operation (RBF replacement, accidental double-spend) and requires no special privilege. The pool is a public-facing service. [9](#0-8) 

---

### Recommendation

Move the link-removal step **inside** `remove_entry`, or update ancestors' `evict_key` explicitly before stripping links. The simplest fix is to collect the ancestor sets for all entries in the batch **before** any links are removed, then apply the `sub_descendant_weight` updates to those ancestors after the batch removal completes:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Collect ancestor sets BEFORE stripping links
    let ancestor_map: Vec<(ProposalShortId, HashSet<ProposalShortId>)> = removed_ids
        .iter()
        .map(|rid| (rid.clone(), self.links.calc_ancestors(rid)))
        .collect();

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Now update ancestors' evict_key for each removed entry
    for (removed_entry, ancestors) in removed.iter().zip(ancestor_map.iter().map(|(_, a)| a)) {
        for anc_id in ancestors {
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

**Setup**: Three transactions in the pool forming a chain: `tx0 → tx1 → tx2` (tx0 is parent of tx1, tx1 is parent of tx2). All three are in `Pending` status.

**State before**:
- `tx0.evict_key` reflects `descendants_count = 3`, `descendants_fee = fee0 + fee1 + fee2`

**Trigger**: Submit `tx_conflict` via RPC `send_transaction`, spending the same input as `tx1`. This calls `resolve_conflict(tx_conflict)` → `remove_entry_and_descendants(tx1)`.

**Execution trace**:
1. `removed_ids = [tx1, tx2]`
2. `remove_entry_links(tx1)` → tx0's children list loses tx1; tx1's link entry deleted
3. `remove_entry_links(tx2)` → tx2's link entry deleted
4. `remove_entry(tx1)`:
   - `update_ancestors_index_key(tx1, Remove)`: `calc_ancestors(tx1)` = **∅** (links gone) → **tx0 not updated**
   - `update_descendants_index_key(tx1, Remove)`: `calc_descendants(tx1)` = **∅** → no-op
5. `remove_entry(tx2)`: same — no updates

**State after**:
- `tx0.evict_key` still has `descendants_count = 3`, `descendants_fee = fee0 + fee1 + fee2`
- Actual state: tx0 has no descendants; correct values would be `descendants_count = 1`, `descendants_fee = fee0`

**Observable impact**: Submit a new transaction `tx_new` that causes the pool to exceed `max_tx_pool_size`. `limit_size` calls `next_evict_entry`, which iterates by `evict_key`. `tx0` appears at a higher eviction priority position than its true standalone fee-rate warrants (or lower, depending on the fee structure of the removed descendants), causing the wrong transaction to be evicted. [10](#0-9)

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

**File:** tx-pool/src/component/pool_map.rs (L607-625)
```rust
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
