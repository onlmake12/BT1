The code confirms the claim. Let me trace through the exact execution:

**`remove_entry_and_descendants`** (lines 252-265): Phase 1 calls `remove_entry_links(id)` for every entry in `removed_ids`, including the root. `remove_entry_links` calls `self.links.remove(id)` which deletes the root's entry from `self.links.inner` entirely.

**`remove_entry`** (lines 235-250): Then calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`.

**`update_ancestors_index_key`** (lines 432-434): Calls `self.links.calc_ancestors(&child.proposal_short_id())`. `calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)` — but the root's entry was already removed from `self.links.inner` in Phase 1, so this returns `None` → empty set. The loop body never executes. Ancestors' `descendants_*` fields and `evict_key` are never updated.

The bug is confirmed exactly as described.

---

Audit Report

## Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` strips all parent/child links for every entry in the removed subtree before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors through those same links, the call inside `remove_entry` finds an empty ancestor set for the root entry and never decrements `descendants_fee`, `descendants_size`, `descendants_cycles`, or `descendants_count` on surviving pool ancestors. Those ancestors retain stale, inflated `evict_key` values, corrupting the pool's eviction ordering and causing `limit_size` to skip low-fee ancestors and instead evict genuinely higher-fee transactions.

## Finding Description
`remove_entry_and_descendants` operates in two phases:

**Phase 1** iterates over the root and all its descendants and calls `remove_entry_links` on each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);
}
```

`remove_entry_links` calls `self.links.remove(id)`, which deletes the entry from `self.links.inner` entirely, and also removes the root from its parents' children sets and from its children's parents sets. [1](#0-0) 

**Phase 2** calls `remove_entry(id)` for each removed entry. Inside `remove_entry`, the first operation is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
``` [2](#0-1) 

`update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

`calc_ancestors` calls `calc_relative_ids`, which does `self.inner.get(short_id)`. Since Phase 1 already called `self.links.remove(id)` for the root, `self.inner.get(root_id)` returns `None`, yielding an empty set. The `for anc_id in &ancestors` loop never executes. [4](#0-3) 

The surviving ancestors (parents of the root that remain in the pool) never receive `sub_descendant_weight` calls, so their `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and `evict_key` remain inflated to include the now-removed subtree.

The `evict_key` is computed from `descendants_fee` and `descendants_size/cycles`: [5](#0-4) 

## Impact Explanation
`next_evict_entry` iterates entries in ascending `evict_key` order: [6](#0-5) 

A stale, inflated `evict_key` on an ancestor makes it appear harder to evict than it deserves. `limit_size` calls `next_evict_entry` in a loop and evicts the selected entry and its descendants: [7](#0-6) 

The result is that a low-fee ancestor transaction survives pool pressure it should not survive, while legitimate higher-fee transactions submitted by other users are rejected with `Reject::Full`. This constitutes incorrect tx-pool eviction behavior exploitable by any unprivileged `send_transaction` caller, fitting **Medium (2001–10000 points)** as an incorrect implementation of the CKB node's transaction pool management mechanism with concrete economic impact on users whose valid high-fee transactions are displaced.

## Likelihood Explanation
Any unprivileged user can trigger this via the `send_transaction` RPC:
1. Submit parent tx `P` with a low fee rate.
2. Submit children `C1…Cn` spending `P`'s outputs with high fee rates, boosting `P`'s `descendants_fee` and `evict_key`.
3. Submit an RBF replacement spending the same inputs as `C1…Cn` (valid when `min_rbf_rate > min_fee_rate`, a common configuration). This triggers `resolve_conflict` → `remove_entry_and_descendants`, leaving `P`'s `evict_key` stale.
4. Flood the pool with small transactions to exceed `max_tx_pool_size` and trigger `limit_size`.
5. `P` is not evicted; other users' high-fee transactions are rejected.

The path through `resolve_conflict` → `remove_entry_and_descendants` is directly reachable: [8](#0-7) 

Additional trigger paths include `resolve_conflict_header_dep`, `check_and_record_ancestors`, and `limit_size` itself (recursive eviction).

## Recommendation
Before stripping links, explicitly update the ancestors of the root entry. The root's ancestors remain in the pool and must have their descendant weights decremented before the link from root to its parents is destroyed:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors of the root BEFORE links are torn down
    if let Some(root_entry) = self.entries.get_by_id(id) {
        self.update_ancestors_index_key(&root_entry.inner.clone(), EntryOp::Remove);
    }

    // strip links so remove_entry won't re-run update_descendants_index_key
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

This mirrors the correct ordering in the single-entry `remove_entry` path, which calls `update_ancestors_index_key` before `remove_entry_links`. [2](#0-1) 

## Proof of Concept
**Setup:** Pool with chain `tx1 → tx2 → tx3` (each with `fee=100, size=100`). After insertion, `tx1.descendants_fee = 300`, `tx1.descendants_count = 3`, `tx1.evict_key` reflects a high descendants fee rate.

**Trigger:** Call `pool_map.remove_entry_and_descendants(&tx2_id)`.

**Expected:** `tx1.descendants_fee = 100`, `tx1.descendants_count = 1`, `tx1.evict_key` recomputed to reflect its lone fee rate.

**Actual:** `tx1.descendants_fee` remains `300`, `tx1.descendants_count` remains `3`, `tx1.evict_key` unchanged.

**Verification:** A unit test can assert that after `remove_entry_and_descendants(&tx2_id)`, `pool_map.get(&tx1_id).unwrap().descendants_count == 1` and `pool_map.get(&tx1_id).unwrap().descendants_fee == Capacity::shannons(100)`. The existing test `test_remove_entry` in `tx-pool/src/component/tests/score_key.rs` covers only the single-entry `remove_entry` path; no equivalent test exists for the multi-entry path. [9](#0-8)

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
