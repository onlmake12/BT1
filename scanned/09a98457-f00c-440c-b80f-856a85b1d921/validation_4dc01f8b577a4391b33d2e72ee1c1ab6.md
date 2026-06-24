Audit Report

## Title
Stale Descendant-Weight Fields in `remove_entry_and_descendants` Corrupts Ancestor `EvictKey` — (File: tx-pool/src/component/pool_map.rs)

## Summary
`PoolMap::remove_entry_and_descendants` calls `remove_entry_links` on every entry being removed before calling `remove_entry` on each. Because `update_ancestors_index_key` inside `remove_entry` resolves ancestors via the live link graph, and those links are already gone, surviving ancestors' `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are never decremented. The stale values inflate the ancestor's `EvictKey`, causing it to be skipped during pool eviction even when its own fee rate is low.

## Finding Description

**Step 1 — Pre-removal of all links**

`remove_entry_and_descendants` collects the target and all its descendants, then strips every link entry before any `remove_entry` call:

```rust
// pool_map.rs L252-265
for id in &removed_ids {
    self.remove_entry_links(id);   // removes id from self.links entirely
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
``` [1](#0-0) 

`remove_entry_links` calls `self.links.remove(id)` as its final step, deleting the entry from `TxLinksMap::inner`: [2](#0-1) 

**Step 2 — `update_ancestors_index_key` finds nothing**

When `remove_entry` is subsequently called, it invokes `update_ancestors_index_key`: [3](#0-2) 

`update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`: [4](#0-3) 

`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)` returns `None` because the link entry was already removed. The result is an empty set; `sub_descendant_weight` is never called on any surviving ancestor. [5](#0-4) 

**Step 3 — Stale fields corrupt `EvictKey`**

`EvictKey` is computed directly from the now-stale `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`: [6](#0-5) 

**Concrete exploit path:**
1. Attacker submits parent tx **A** with a low fee.
2. Attacker submits child tx **B** with a high fee (child of A). This causes `add_descendant_weight` to be called on A, inflating A's `descendants_fee` and `descendants_cycles`.
3. Attacker submits tx **C** that double-spends one of B's inputs. `resolve_conflict` calls `remove_entry_and_descendants(B)`. Due to the bug, A's `descendants_*` fields are not decremented.
4. A now has an artificially high `EvictKey` (reflecting B's fee rate). `next_evict_entry` iterates by `evict_key` ascending and skips A.
5. When the pool is full, `limit_size` evicts other transactions instead of A, even if A's own fee rate is the lowest in the pool. [7](#0-6) 

**No existing guard prevents this.** The comment in `remove_entry_and_descendants` explicitly acknowledges the pre-removal of links is intentional to suppress `update_descendants_index_key`, but it silently also suppresses `update_ancestors_index_key`, which is the unintended side-effect.

## Impact Explanation

A surviving ancestor transaction permanently carries inflated descendant-weight statistics. Its `EvictKey` overstates its effective fee rate. When the pool is full, the eviction loop (`limit_size`) picks the entry with the lowest `EvictKey`; the ancestor with the artificially high key is skipped. Legitimate high-fee transactions submitted by other users may be evicted in its place, or rejected outright with `Reject::Full`. This matches the allowed impact: **Medium (2001–10000 points) — Suboptimal implementation of CKB state storage mechanism**, specifically incorrect eviction-key accounting in the tx-pool that degrades fee-priority ordering for all pool participants.

## Likelihood Explanation

Any unprivileged user can trigger this with two transactions (a parent and a child) plus one conflicting transaction to remove the child. No special privileges, leaked keys, or external dependencies are required. The conflicting transaction can be crafted by the attacker themselves since they control the child's inputs. The bug is deterministic and repeatable: every call to `remove_entry_and_descendants` where the removed subtree has surviving ancestors outside the removed set will leave those ancestors with stale fields. This includes `resolve_conflict`, `resolve_conflict_header_dep`, and `limit_size` call sites.

## Recommendation

In `remove_entry_and_descendants`, before stripping links, update surviving ancestors' descendant-weight counters. One correct approach: for each entry being removed, call `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` **before** calling `remove_entry_links`. Alternatively, restructure the function to only pre-remove links for entries whose ancestors are also in the removed set (i.e., skip the ancestor update only when the ancestor is itself being removed), and let `remove_entry` handle the ancestor update normally for entries whose ancestors survive.

## Proof of Concept

**Minimal unit test plan (in `tx-pool/src/component/pool_map.rs` test module):**

1. Create a `PoolMap` and insert two entries: parent `A` (low fee) and child `B` (high fee, spending an output of A). Verify `A.descendants_fee == A.fee + B.fee`.
2. Call `pool_map.remove_entry_and_descendants(&B.proposal_short_id())`.
3. Assert that `A` is still in the pool.
4. Assert `pool_map.get(&A_id).descendants_fee == A.fee` (i.e., B's fee has been subtracted). **This assertion will fail**, demonstrating the bug: `descendants_fee` still equals `A.fee + B.fee`.
5. Compute `A.as_evict_key()` and compare it to the key computed from correct (decremented) fields to show the `EvictKey` is inflated.

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
