Audit Report

## Title
Stale Descendant-Weight Tracking in `remove_entry_and_descendants` Corrupts Eviction Ordering — (File: tx-pool/src/component/pool_map.rs)

## Summary
`PoolMap::remove_entry_and_descendants` pre-clears all link records via `remove_entry_links` for every transaction in the removal set before calling `remove_entry` on each one. Because `update_ancestors_index_key` resolves surviving ancestors through `self.links.calc_ancestors`, and those links have already been erased, the ancestor-update loop never executes. Every pool entry that is an ancestor of the removed subtree root retains permanently inflated `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles`, corrupting the `EvictKey` used to select eviction candidates.

## Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, then calls `remove_entry_links` on every member of that set before any entry is actually removed: [1](#0-0) 

`remove_entry_links` removes the entry from `self.links.inner` entirely via `self.links.remove(id)`: [2](#0-1) 

When `remove_entry` is subsequently called, it invokes `update_ancestors_index_key` at line 242: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because `remove_entry_links` already called `self.links.remove(id)`, the entry is absent from `self.links.inner`, so `calc_relative_ids` returns an empty `direct` set and `calc_relation_ids` returns an empty `HashSet`. The loop body never executes: [4](#0-3) [5](#0-4) 

The surviving ancestors' `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` are never decremented. The `EvictKey` stored in each `PoolEntry` is therefore computed from stale, inflated values: [6](#0-5) 

The code comment at line 256 reveals the intent — to suppress `update_descendants_index_key` for entries being removed — but the pre-clearing of links also silently disables `update_ancestors_index_key` for surviving ancestors, which is the unintended side effect causing the bug. The single-entry path `remove_entry` (called directly) does not pre-clear links, so it correctly updates ancestors.

## Impact Explanation

`EvictKey` ordering is ascending — the entry with the smallest key is evicted first: [7](#0-6) 

A surviving ancestor whose descendants were removed via `remove_entry_and_descendants` retains an artificially high `fee_rate` (from the removed descendants' fees) and an artificially high `descendants_count`. Both push its `EvictKey` higher, so it is evicted later than warranted. `limit_size`, the sole pool-size enforcement path, relies entirely on this ordering: [8](#0-7) 

An attacker can keep a low-fee transaction permanently shielded from eviction, filling the pool with low-fee transactions and blocking legitimate higher-fee transactions from entering. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

The trigger is fully reachable by any unprivileged transaction sender whenever `min_rbf_rate > min_fee_rate` (the recommended production setting). The attacker pays only the RBF fee delta per cycle, which can be set to the minimum increment. The same stale-state condition is also triggered by `resolve_conflict_header_dep`, `remove_by_detached_proposal`, and `remove_tx`, all of which call `remove_entry_and_descendants`. The attack is repeatable indefinitely at low cost.

## Recommendation

Before pre-clearing links, snapshot the surviving ancestors of the root entry, then after removal explicitly call `sub_descendant_weight` on each survivor for every removed entry:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Snapshot surviving ancestors BEFORE clearing links
    let surviving_ancestors: HashSet<ProposalShortId> = self
        .links
        .calc_ancestors(id)
        .into_iter()
        .filter(|anc| !removed_ids.contains(anc))
        .collect();

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Correct surviving ancestors' descendant weights
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

## Proof of Concept

**Preconditions:** pool near `max_tx_pool_size`; `min_rbf_rate > min_fee_rate`.

1. Submit tx **A** (low fee, e.g. 1 shannon/byte). `A.descendants_count = 1`, `A.EvictKey.fee_rate = low`.
2. Submit tx **B** spending A's output (high fee, e.g. 1000 shannon/byte). `record_entry_descendants` → `update_ancestors_index_key(B, Add)` → `A.descendants_count = 2`, `A.descendants_fee += B.fee`, `A.EvictKey.fee_rate = high`.
3. Submit tx **B′** conflicting with B, fee > B.fee (satisfies RBF). `resolve_conflict` → `remove_entry_and_descendants(&B_id)`:
   - `remove_entry_links(B_id)` erases B's link record from `self.links.inner`.
   - `remove_entry(B_id)` → `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B_id)` returns `{}` → **A is not updated**.
   - A retains `descendants_count = 2`, `descendants_fee = A.fee + B.fee`, `EvictKey.fee_rate = high`.
4. Pool fills. `limit_size` → `next_evict_entry` iterates ascending by `EvictKey`. A's inflated key places it after genuinely high-fee transactions; A is not evicted.
5. Repeat steps 2–3 to keep A's `EvictKey` perpetually inflated. Net cost per cycle is only the RBF fee delta.

A unit test can assert that after steps 2–3, `pool_map.get_by_id(&A_id).unwrap().evict_key` equals `A.as_evict_key()` computed with `descendants_count = 1` and `descendants_fee = A.fee` only — the test will fail on the current code, confirming the bug.

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

**File:** tx-pool/src/component/sort_key.rs (L92-103)
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
