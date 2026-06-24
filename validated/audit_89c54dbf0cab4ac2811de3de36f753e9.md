Audit Report

## Title
Stale Descendant-Weight Fields on Surviving Pool Ancestors After `remove_entry_and_descendants` — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-strips all `TxLinksMap` entries for the target and its entire descendant set before calling `remove_entry` on each node. Because `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`, and those links are already gone by the time `remove_entry` runs, any pool transaction that is an ancestor of the removed subtree root but is not itself in the removed set never receives `sub_descendant_weight` calls. Its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are permanently inflated, corrupting `EvictKey` ordering for all subsequent eviction decisions.

## Finding Description

**Root cause — `remove_entry_and_descendants` (lines 252–265):**

`remove_entry_and_descendants` collects `removed_ids = [id] + calc_descendants(id)`, then pre-strips every node's links via `remove_entry_links` before calling `remove_entry` on each. [1](#0-0) 

`remove_entry_links` (lines 418–430) removes the node's own entry from `TxLinksMap.inner` and cross-references from its parents and children. After the pre-strip loop, every node in `removed_ids` is absent from `self.links`. [2](#0-1) 

**Why surviving ancestors are never updated:**

`remove_entry` (line 242) calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`. [3](#0-2) 

`update_ancestors_index_key` (lines 432–445) calls `self.links.calc_ancestors(&child.proposal_short_id())`. [4](#0-3) 

`calc_ancestors` delegates to `calc_relative_ids` (links.rs lines 37–50), which looks up the node in `self.inner`. Since the pre-strip phase already called `self.links.remove(id)` for every node in `removed_ids`, `self.inner.get(short_id)` returns `None`, `direct` is empty, and `calc_relation_ids` returns `{}`. [5](#0-4) 

`sub_descendant_weight` is therefore never called on any surviving ancestor. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain inflated. [6](#0-5) 

**Concrete scenario — chain X → A → B → C:**

Pool contains X (surviving ancestor) → A → B → C. A block commits a transaction that double-spends A's input. `resolve_conflict` (lines 305–316) calls `remove_entry_and_descendants(&A_id)`. `removed_ids = [A, B, C]`. All three links are stripped. When `remove_entry(A/B/C)` runs, `calc_ancestors` returns `{}` for each. X's `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles` are never decremented and remain inflated by the combined weight of A, B, and C. [7](#0-6) 

**`EvictKey` construction (entry.rs lines 234–247):**

All three fields of `EvictKey` — `fee_rate` (via stale `descendants_fee`/`descendants_size`/`descendants_cycles`) and `descendants_count` — are derived from the stale fields. X's stored `evict_key` is never refreshed after the removal. [8](#0-7) 

**Note on fee-rate estimate claim:** The submitted report's claim that `estimate_fee_rate` is affected is incorrect. `pool_map::estimate_fee_rate` iterates by `score` (`AncestorsScoreSortKey`), which is based on ancestor weights, not descendant weights. [9](#0-8) 

## Impact Explanation

`next_evict_entry` (lines 380–385) iterates by `evict_key`. [10](#0-9) 

An inflated `descendants_feerate` in X's stale `evict_key` makes X appear more valuable than it actually is, placing it later in the eviction order than it should be. X survives eviction rounds it should lose; legitimate higher-value transactions may be evicted in its place. This is a persistent corruption: the stale fields are never corrected after the removal, so every subsequent eviction decision involving X is wrong until X itself is eventually removed.

**Impact class: Low (501–2000 points) — important performance/correctness improvement for CKB tx-pool eviction.**

## Likelihood Explanation

The trigger requires two steps: (1) submit a chain X → A → B → C via standard P2P/RPC (fully unprivileged), and (2) have a transaction conflicting with A included in a block. Step 2 requires either mining power or a miner that accepts out-of-pool transactions. The bug also fires naturally whenever any block contains a transaction that conflicts with a pool transaction that itself has pool ancestors — a routine occurrence on mainnet during normal operation. The bug is therefore reachable without deliberate attack, and repeatable across every such block event.

## Recommendation

In `remove_entry_and_descendants`, update surviving ancestors' descendant-weight fields **before** stripping links. The minimal fix: for each node being removed, call `update_ancestors_index_key(node, EntryOp::Remove)` while links are still intact, then strip the links. Alternatively, collect the set of surviving ancestors (those not in `removed_ids`) before any stripping and explicitly call `sub_descendant_weight` on them for each removed node, then update their `evict_key`.

The existing comment `// update links state for remove, so that we won't update_descendants_index_key in remove_entry` correctly explains the intent (avoid updating already-removed descendants' `score`), but the implementation also inadvertently suppresses the necessary ancestor `evict_key` updates. [11](#0-10) 

## Proof of Concept

1. Build a `PoolMap` with chain **X → A → B → C** (each spending the previous tx's output). Add all four as `Pending` entries.
2. Record `X.descendants_count` (expected: 4, counting X itself) and `X.descendants_fee` (expected: sum of X+A+B+C fees).
3. Call `pool_map.remove_entry_and_descendants(&A_id)`.
4. Assert `pool_map.contains_key(&X_id)` is `true` (X survives).
5. Retrieve X's `PoolEntry` and assert `entry.inner.descendants_count == 1` — **this assertion fails**; the field still reads 4.
6. Assert `entry.inner.descendants_fee == X.fee` (only X's own fee) — **this assertion fails**; the field retains the inflated sum.
7. Assert `entry.evict_key == X.inner.as_evict_key()` after recomputing from correct fields — **this assertion fails**; the stored `evict_key` reflects the stale descendant weights.
8. Call `pool_map.next_evict_entry(Status::Pending)` and observe that X is ordered incorrectly relative to other pool entries with accurate `evict_key` values.

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

**File:** tx-pool/src/component/pool_map.rs (L334-359)
```rust
    pub(crate) fn estimate_fee_rate(
        &self,
        mut target_blocks: usize,
        max_block_bytes: usize,
        max_block_cycles: Cycle,
        min_fee_rate: FeeRate,
    ) -> FeeRate {
        debug_assert!(target_blocks > 0);
        let iter = self.entries.iter_by_score().rev();
        let mut current_block_bytes = 0;
        let mut current_block_cycles = 0;
        for entry in iter {
            current_block_bytes += entry.inner.size;
            current_block_cycles += entry.inner.cycles;
            if current_block_bytes >= max_block_bytes || current_block_cycles >= max_block_cycles {
                target_blocks -= 1;
                if target_blocks == 0 {
                    return entry.inner.fee_rate();
                }
                current_block_bytes = entry.inner.size;
                current_block_cycles = entry.inner.cycles;
            }
        }

        min_fee_rate
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
