Audit Report

## Title
Stale `descendants_fee` After Subtree Removal Enables Pool Eviction Bypass - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-removes all link entries for the entire subtree before calling `remove_entry` on each node. When `remove_entry` subsequently calls `update_ancestors_index_key`, it invokes `calc_ancestors` on an entry whose link record has already been deleted, returning an empty ancestor set. Surviving ancestors of the removed subtree therefore never receive `sub_descendant_weight`, leaving their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` permanently inflated, which inflates their `EvictKey.fee_rate` and makes them immune to pool eviction.

## Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, pre-removes every link entry, then calls `remove_entry` on each: [1](#0-0) 

The comment on line 256 explains the intent: pre-removing links prevents `update_descendants_index_key` from redundantly updating entries that are themselves being removed. However, this also destroys the data that `update_ancestors_index_key` needs.

Inside `remove_entry`, the ancestor update path calls `update_ancestors_index_key`: [2](#0-1) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

`calc_ancestors` delegates to `calc_relative_ids`, which first looks up the entry in `self.inner`: [4](#0-3) 

Because `remove_entry_links` already called `self.links.remove(id)` for the removed entry, `self.inner.get(short_id)` returns `None`, `direct` becomes an empty `HashSet`, and `calc_relation_ids` returns an empty set. The loop in `update_ancestors_index_key` iterates over nothing. Surviving ancestors (e.g., a parent `P` of the removed root `C`) never have `sub_descendant_weight` called on them.

`remove_entry_links` removes the entry's own record and severs bidirectional links: [5](#0-4) 

`sub_descendant_weight` uses `saturating_sub`, so any future underflow would silently zero the field rather than panic or signal an error: [6](#0-5) 

The stale `descendants_fee` directly feeds `EvictKey` computation via `descendants_feerate.max(feerate)`: [7](#0-6) 

`EvictKey` ordering is ascending by `fee_rate`, so `next_evict_entry` selects the entry with the lowest `fee_rate` first: [8](#0-7) 

`resolve_conflict` — the RBF path — calls `remove_entry_and_descendants`, making this reachable by any tx-pool submitter: [9](#0-8) 

The pool size-limit eviction loop also calls `remove_entry_and_descendants`, so the same stale accounting affects eviction under memory pressure: [10](#0-9) 

`total_tx_size` has a `recompute_total_stat` correction path on underflow, but `descendants_fee` has no equivalent correction mechanism: [11](#0-10) 

## Impact Explanation

A surviving ancestor retains an inflated `descendants_fee`. Its `EvictKey` is computed as `descendants_feerate.max(feerate)`. With an artificially high `descendants_feerate`, the entry ranks as highly valuable and is never selected for eviction by `next_evict_entry`. An attacker can fill the pool with such entries, causing all subsequent legitimate transaction submissions to be rejected with `Reject::Full`. This constitutes a low-cost, repeatable DoS attack on the CKB transaction propagation layer, matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs* (10001–15000 points).

## Likelihood Explanation

Any node's RPC `send_transaction` endpoint accepts submissions from unprivileged callers. RBF is enabled whenever `min_rbf_rate > min_fee_rate`. The attacker pays incrementally higher fees per replacement (RBF requirement), but each replacement permanently inflates the parent's `descendants_fee` by the replaced child's fee. After N replacements the inflation is `sum(child_fees[0..N-1])`, which grows without bound. No privileged access, key material, or majority hashpower is required. The attack is repeatable and cheap relative to the damage caused.

## Recommendation

Before calling `remove_entry_links` for any entry in the removed subtree, compute the surviving ancestors of the root entry and apply `sub_descendant_weight` for each removed entry against those ancestors. Concretely, in `remove_entry_and_descendants`, call `calc_ancestors(id)` for the root **before** any link removal to obtain the set of surviving ancestors, then for each entry in `removed_ids` iterate over those ancestor IDs and call `sub_descendant_weight` with the removed entry's weight and update `evict_key`. This mirrors the logic already present in `update_ancestors_index_key` but executed before links are torn down.

## Proof of Concept

```rust
// Unit test outline (add to tx-pool/src/component/tests/):
// 1. Build P (low fee, e.g. 100 shannons) with no pool parents.
// 2. Build C1 (high fee, e.g. 10_000 shannons) spending P's output[0].
// 3. pool.add_proposed(P); pool.add_proposed(C1);
//    Assert: pool.get(P_id).descendants_fee == P.fee + C1.fee
//
// 4. Build C1' (higher fee, e.g. 20_000 shannons) spending the same input as C1.
//    Call pool.resolve_conflict(&C1_tx) → triggers remove_entry_and_descendants(C1_id).
//    Then pool.add_proposed(C1');
//
// 5. Assert (BUG): pool.get(P_id).descendants_fee == P.fee + C1.fee + C1'.fee
//    Expected (correct): pool.get(P_id).descendants_fee == P.fee + C1'.fee
//
// 6. Repeat step 4 N times with C2, C3, ..., CN (each with higher fee).
//    Assert: P.descendants_fee grows by sum(C1..CN-1 fees) beyond correct value.
//    Assert: P.evict_key.fee_rate is inflated → P is never selected by next_evict_entry.
//
// 7. Fill pool to max_tx_pool_size with unrelated low-fee transactions.
//    Assert: new legitimate transactions are rejected with Reject::Full,
//    while P (with inflated evict_key) remains in the pool.
```

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-243)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
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

**File:** tx-pool/src/component/sort_key.rs (L92-104)
```rust
impl Ord for EvictKey {
    fn cmp(&self, other: &Self) -> Ordering {
        if self.fee_rate == other.fee_rate {
            if self.descendants_count == other.descendants_count {
                self.timestamp.cmp(&other.timestamp)
            } else {
                self.descendants_count.cmp(&other.descendants_count)
            }
        } else {
            self.fee_rate.cmp(&other.fee_rate)
        }
    }
}
```

**File:** tx-pool/src/pool.rs (L298-328)
```rust
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
