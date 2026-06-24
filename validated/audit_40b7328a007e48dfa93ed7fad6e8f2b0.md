The code confirms the bug. Let me trace through it precisely:

**`remove_entry_and_descendants`** (lines 252-265): strips all links for every entry in `removed_ids` via `remove_entry_links`, then calls `remove_entry` on each.

**`remove_entry_links`** (lines 418-430): removes the entry from its parents' children sets, removes it from its children's parents sets, and calls `self.links.remove(id)` — deleting the entry's own record from the links map entirely.

**`remove_entry`** (lines 235-250): immediately calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`.

**`update_ancestors_index_key`** (lines 432-445): calls `self.links.calc_ancestors(&child.proposal_short_id())`. Since `remove_entry_links` already called `self.links.remove(id)`, `calc_relative_ids` hits `unwrap_or_default()` at line 46-47 of `links.rs` and returns an empty set. The loop body never runs. Ancestors' `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and `evict_key` are never decremented.

The bug is confirmed. The impact is real but bounded — it corrupts eviction ordering in the tx pool, not consensus or node stability.

---

Audit Report

## Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` strips all parent/child links for the entire removed subtree before calling `remove_entry` on each entry. Because `update_ancestors_index_key` resolves ancestors through those same links, the call inside `remove_entry` finds an empty ancestor set and never decrements `descendants_fee`, `descendants_size`, `descendants_cycles`, or `descendants_count` on surviving ancestors. Their `evict_key` remains stale and inflated, corrupting the pool's eviction ordering and causing `limit_size` to skip over low-fee ancestors and instead evict genuinely higher-fee transactions.

## Finding Description
`remove_entry_and_descendants` operates in two phases:

**Phase 1** — for every entry in `removed_ids` (root + all descendants), `remove_entry_links` is called:

```rust
// pool_map.rs L257-259
for id in &removed_ids {
    self.remove_entry_links(id);
}
```

`remove_entry_links` (L418-430) removes the entry from its parents' children sets, removes it from its children's parents sets, and calls `self.links.remove(id)` — deleting the entry's own record from the `TxLinksMap` entirely.

**Phase 2** — `remove_entry` is called for each id. Inside `remove_entry` (L242), the first call is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` (L432-434) calls:

```rust
let ancestors: HashSet<ProposalShortId> =
    self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)` in `links.rs` (L42-47). Since Phase 1 already called `self.links.remove(id)`, the entry is gone from the map. `unwrap_or_default()` returns an empty set. The `for anc_id in &ancestors` loop body never executes. No ancestor's `sub_descendant_weight` is called, and no ancestor's `evict_key` is recomputed.

The comment at L256 acknowledges the intent is to skip `update_descendants_index_key` (correct — descendants are all being removed). But it silently also skips `update_ancestors_index_key` for the root entry's ancestors, which remain in the pool and are now carrying stale, inflated descendant accounting.

`EvictKey` is computed from `descendants_fee` and `descendants_size/cycles` (`entry.rs` L234-247). With stale inflated values, the ancestor's `evict_key` ranks it as harder to evict than it deserves.

## Impact Explanation
`next_evict_entry` (L380-385) iterates entries in ascending `evict_key` order and picks the first match by status. `limit_size` (pool.rs L292-329) calls `next_evict_entry` in a loop until `total_tx_size <= max_tx_pool_size`, evicting via `remove_entry_and_descendants`. A stale, inflated `evict_key` on an ancestor causes `limit_size` to skip it and instead evict a genuinely higher-fee transaction, issuing `Reject::Full` to that transaction's submitter.

This matches: **Low (501–2000 points) — Any other important performance/correctness improvement for CKB**, as it corrupts the tx pool's eviction invariant and causes incorrect rejection of legitimate transactions under pool pressure. It does not crash the node or cause consensus deviation.

## Likelihood Explanation
Any unprivileged `send_transaction` RPC caller can reach this path:
1. Submit parent `P` (low fee rate).
2. Submit children `C1…Cn` (high fee rates), boosting `P`'s `descendants_fee` and `evict_key`.
3. Trigger removal of `C1…Cn` via RBF (enabled when `min_rbf_rate > min_fee_rate`, a common configuration per pool.rs L80-83) or via a reorg that detaches the proposal window.
4. `P`'s `evict_key` is now stale/inflated.
5. Fill the pool with many small transactions to trigger `limit_size`.
6. `P` is not evicted; other users' higher-fee transactions are rejected with `Reject::Full`.

`remove_entry_and_descendants` is reachable from `limit_size`, `resolve_conflict`, `resolve_conflict_header_dep`, and `check_and_record_ancestors`, making the trigger surface broad.

## Recommendation
Before stripping links, explicitly update the ancestors of the root entry only (not descendants, since their ancestors are also being removed):

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors of the root entry BEFORE links are torn down
    if let Some(root_entry) = self.entries.get_by_id(id) {
        self.update_ancestors_index_key(&root_entry.inner.clone(), EntryOp::Remove);
    }

    // strip links so that remove_entry won't re-run update_descendants_index_key
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

This mirrors the single-entry `remove_entry` path (L242-243), which correctly calls both `update_ancestors_index_key` and `update_descendants_index_key` before removing links.

## Proof of Concept
**Setup:** Pool with chain `tx1 → tx2 → tx3` (tx2 spends tx1's output, tx3 spends tx2's output). All have `fee=100, size=100`. After insertion, `tx1.descendants_fee = 300`, `tx1.descendants_count = 3`, `tx1.evict_key` reflects a high descendants fee rate.

**Trigger:** Call `pool_map.remove_entry_and_descendants(&tx2_id)` (removes tx2 and tx3).

**Expected:** `tx1.descendants_fee` drops to `100`, `tx1.descendants_count` drops to `1`, `tx1.evict_key` is recomputed to reflect its actual lone fee rate.

**Actual:** `tx1.descendants_fee` remains `300`, `tx1.descendants_count` remains `3`, `tx1.evict_key` is unchanged.

**Verification:** Add a unit test to `tx-pool/src/component/tests/score_key.rs` (which already tests `remove_entry` for `ancestors_count` correctness) asserting that after `remove_entry_and_descendants(&tx2_id)`, `pool_map.get(&tx1_id).unwrap().descendants_count == 1` and `pool_map.get(&tx1_id).unwrap().descendants_fee == fee_of_tx1`. This test will fail on the current code and pass after the fix. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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
