Audit Report

## Title
Ancestor Descendant-Weight Not Decremented After `remove_entry_and_descendants` Pre-Clears Links — (`tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` pre-clears all link entries via `remove_entry_links` before calling `remove_entry` on each removed transaction. When the removed transaction has surviving ancestors in the pool, `update_ancestors_index_key` inside `remove_entry` calls `calc_ancestors` on the already-removed ID, receives an empty set, and never invokes `sub_descendant_weight` on those ancestors. The result is permanently stale, inflated `descendants_*` fields and `evict_key` on any ancestor that remains in the pool after the removal.

## Finding Description

`remove_entry_and_descendants` collects all descendants, then calls `remove_entry_links` for every ID in the set before calling `remove_entry` on any of them: [1](#0-0) 

`remove_entry_links` severs all parent/child cross-references and removes the entry from `links.inner`: [2](#0-1) 

`remove_entry` then calls `update_ancestors_index_key`, which calls `calc_ancestors` on the now-absent ID: [3](#0-2) 

`calc_ancestors` delegates to `calc_relative_ids`, which does `self.inner.get(short_id)` — returning `None` since the entry was already removed — and returns an empty set: [4](#0-3) 

The `for anc_id in &ancestors` loop in `update_ancestors_index_key` never executes; `sub_descendant_weight` and the `evict_key` refresh are skipped for all surviving ancestors: [5](#0-4) 

The comment at L256 (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) confirms the pre-clearing is intentional to suppress descendant updates for co-removed txs, but it inadvertently also suppresses ancestor updates for txs that are **not** being removed.

The reachable trigger path is `resolve_conflict` → `remove_entry_and_descendants`, called on every confirmed block that conflicts with a child transaction whose parent remains in the pool: [6](#0-5) 

## Impact Explanation

`EvictKey.fee_rate` is computed as `descendants_feerate.max(feerate)`, where `descendants_feerate` is derived from the stale inflated `descendants_fee`, `descendants_size`, and `descendants_cycles`: [7](#0-6) 

With inflated `descendants_*`, a surviving ancestor's `EvictKey.fee_rate` is higher than its true value. Since `next_evict_entry` iterates by `evict_key` ascending, the ancestor is pushed toward the back of the eviction queue and protected from eviction it should be subject to. Transactions with genuinely higher fee rates may be incorrectly evicted in its place. The stale `evict_key` stored in the `MultiIndexPoolEntryMap` is never corrected until the ancestor itself is removed. This constitutes a **suboptimal implementation of the CKB state storage mechanism** (Medium, 2001–10000 points), as the mempool's accounting invariant is permanently broken for affected entries until they are naturally removed. [8](#0-7) 

## Likelihood Explanation

This triggers on every confirmed block that conflicts with a child transaction whose parent remains in the pool — a routine occurrence during normal chain operation. No special attacker capability is needed beyond submitting a parent-child tx chain and waiting for (or causing) a block that conflicts with the child. The bug is deterministic and locally reproducible without any privileged access, majority hashpower, or leaked keys.

## Recommendation

In `remove_entry_and_descendants`, update surviving ancestors' descendant weights **before** clearing links. One concrete fix: for each ID in `removed_ids`, call `update_ancestors_index_key` first (while links are still intact and ancestors are still resolvable), then call `remove_entry_links` for all, then proceed with the rest of `remove_entry` cleanup. Alternatively, pass the set of removed IDs into `update_ancestors_index_key` so it can skip IDs that are also being removed, avoiding the need to pre-clear links. [1](#0-0) 

## Proof of Concept

```
1. Add tx_parent to pool (descendants_count = 1, descendants_fee = F_p)
2. Add tx_child spending tx_parent's output
   → tx_parent.descendants_count = 2, descendants_fee = F_p + F_c
3. Call pool_map.remove_entry_and_descendants(&tx_child_id)
   - remove_entry_links(tx_child_id): removes tx_child from links.inner,
     removes tx_child from tx_parent's children set
   - remove_entry(tx_child_id): calls update_ancestors_index_key(tx_child, Remove)
       calc_ancestors(tx_child_id) → {} (empty; tx_child already gone from links.inner)
       sub_descendant_weight never called on tx_parent
4. Assert: tx_parent.descendants_count == 1  → FAILS, still 2
5. Assert: tx_parent.descendants_fee == F_p  → FAILS, still F_p + F_c
6. Assert: tx_parent.evict_key reflects only tx_parent's own fee rate → FAILS, still inflated
```

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L305-316)
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
