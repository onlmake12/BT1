Looking at the actual code to verify each claim in the report.

Audit Report

## Title
Stale `descendants_fee`/`descendants_size` After `remove_entry_and_descendants` Corrupts Eviction Priority — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::remove_entry_and_descendants`, all parent/child links are severed via `remove_entry_links` for every transaction in the removal batch **before** `remove_entry` is called on each. Because `update_ancestors_index_key` relies on `self.links.calc_ancestors` to find which surviving pool entries need their `descendants_*` fields decremented, and those links are already gone at call time, the surviving ancestors of the removed subtree are never updated. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain permanently inflated, corrupting the `EvictKey` used to rank transactions for eviction when the pool is full.

## Finding Description

`remove_entry_and_descendants` collects the root ID and all its descendants, then calls `remove_entry_links` for every ID in the batch before calling `remove_entry`: [1](#0-0) 

`remove_entry_links` removes the entry from `self.links.inner` entirely: [2](#0-1) 

When `remove_entry` is subsequently called, it invokes `update_ancestors_index_key`: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors`, which looks up the entry in `self.links.inner`: [4](#0-3) 

`calc_ancestors` delegates to `calc_relative_ids`, which starts by fetching the entry's direct parents from `self.links.inner`: [5](#0-4) 

Since `remove_entry_links` already called `self.links.remove(id)`, the lookup returns `None` and `calc_ancestors` returns an empty set. The `sub_descendant_weight` call that should decrement `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` on surviving ancestors is never executed: [6](#0-5) 

The single-entry path (`remove_entry` called directly) does **not** have this bug because `remove_entry_links` is called **after** `update_ancestors_index_key` at line 245, so `calc_ancestors` still finds the correct ancestors at the time it is called. [7](#0-6) 

The stale fields feed directly into `EvictKey` computation: [8](#0-7) 

`EvictKey` is ordered ascending by `fee_rate`; the entry with the lowest key is evicted first. An ancestor with an inflated `descendants_fee` has an artificially high `fee_rate` in its `EvictKey`, placing it later in the eviction order and making it eviction-resistant. [9](#0-8) 

## Impact Explanation

`next_evict_entry` iterates `iter_by_evict_key()` in ascending order to select the next transaction to drop: [10](#0-9) 

`limit_size` calls this in a loop until the pool is within bounds: [11](#0-10) 

A surviving ancestor with a stale (inflated) `EvictKey.fee_rate` is skipped during eviction. Legitimate medium- or high-fee transactions are evicted in its place. An attacker can exploit this to keep a near-zero-fee transaction in the pool indefinitely, occupying pool space and forcing eviction of legitimate transactions. Repeated across multiple slots, this constitutes **CKB network congestion achievable with few costs**, matching the **High** impact class (10001–15000 points).

## Likelihood Explanation

The exploit requires only two `send_transaction` RPC calls followed by one RBF replacement, all available to any unprivileged peer. `remove_entry_and_descendants` is reachable from four code paths:

1. `resolve_conflict` — triggered by any conflicting/RBF transaction submission.
2. `resolve_conflict_header_dep` — triggered by any block reorg invalidating a header dep.
3. `limit_size` — triggered automatically when the pool exceeds `max_tx_pool_size`.
4. `check_and_record_ancestors` — triggered when ancestor count exceeds `max_ancestors_count`. [12](#0-11) [13](#0-12) [14](#0-13) 

The corruption is permanent for the lifetime of the affected entry (until mined or node restart). The cost per "slot" is one RBF replacement fee, making large-scale pool squatting economically feasible.

## Recommendation

Before severing links in `remove_entry_and_descendants`, collect the set of external ancestors (those not in `removed_ids`) and update their `descendants_*` fields first:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();

    // Update surviving ancestors BEFORE severing links
    for removed_id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(removed_id) {
            let entry_inner = entry.inner.clone();
            let ancestors = self.links.calc_ancestors(removed_id);
            for anc_id in ancestors.iter().filter(|a| !removed_set.contains(*a)) {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&entry_inner);
                    e.evict_key = e.inner.as_evict_key();
                });
            }
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Alternatively, restructure `remove_entry` so that `update_ancestors_index_key` is called before `remove_entry_links`, matching the invariant already upheld by the single-entry removal path.

## Proof of Concept

**Setup:**
1. Submit `tx0` (low fee, e.g. 100 shannons) to the pool.
2. Submit `tx1` (high fee, e.g. 10,000 shannons, spending `tx0`'s output). After insertion, `tx0.descendants_fee = 10,100`, `tx0.descendants_count = 2`.

**Trigger:**
3. Submit `tx1'` conflicting with `tx1` (fee > `tx1` fee + RBF minimum bump). `resolve_conflict` calls `remove_entry_and_descendants(&tx1_id)`.

**Observe:**
4. `tx1` is removed. `tx0.descendants_fee` remains `10,100` (should be `100`). `tx0.evict_key.fee_rate` is computed from the inflated `descendants_fee`, making `tx0` appear as a high-fee-rate transaction in the eviction index.

**Exploit:**
5. Fill the pool to capacity with medium-fee transactions. `limit_size` iterates `iter_by_evict_key()` ascending and evicts medium-fee transactions before `tx0`, even though `tx0`'s real fee rate is the lowest in the pool.
6. `tx0` persists indefinitely. Repeat step 3 with a new child to refresh the inflation if needed.

A unit test can assert that after step 3, `pool_map.get(&tx0_id).unwrap().descendants_fee == tx0.fee` and `pool_map.get(&tx0_id).unwrap().descendants_count == 1`.

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

**File:** tx-pool/src/component/pool_map.rs (L267-292)
```rust
    pub(crate) fn resolve_conflict_header_dep(
        &mut self,
        headers: &HashSet<Byte32>,
    ) -> Vec<ConflictEntry> {
        let mut conflicts = Vec::new();

        // invalid header deps
        let mut ids = Vec::new();
        for (tx_id, deps) in self.edges.header_deps.iter() {
            for hash in deps {
                if headers.contains(hash) {
                    ids.push((hash.clone(), tx_id.clone()));
                    break;
                }
            }
        }

        for (blk_hash, id) in ids {
            let entries = self.remove_entry_and_descendants(&id);
            for entry in entries {
                let reject = Reject::Resolve(OutPointError::InvalidHeader(blk_hash.to_owned()));
                conflicts.push((entry, reject));
            }
        }
        conflicts
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

**File:** tx-pool/src/component/pool_map.rs (L614-625)
```rust

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

**File:** tx-pool/src/component/entry.rs (L132-142)
```rust
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

**File:** tx-pool/src/pool.rs (L292-329)
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
    }
```
