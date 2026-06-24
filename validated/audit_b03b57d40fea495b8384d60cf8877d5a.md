Audit Report

## Title
Surviving ancestors retain permanently inflated `descendants_count`/`descendants_fee` after `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-clears all link entries via `remove_entry_links` before calling `remove_entry` on each removed transaction. When `remove_entry` subsequently calls `update_ancestors_index_key`, it queries the already-mutated links map and finds an empty ancestor set, so `sub_descendant_weight` is never called on surviving ancestors. Those ancestors permanently carry stale, inflated descendant weights, corrupting their `EvictKey` and allowing low-fee transactions to evade eviction indefinitely.

## Finding Description

**Root cause — `remove_entry_and_descendants` (lines 252–265):**

The first loop (lines 257–259) calls `remove_entry_links` for every id in `removed_ids`. [1](#0-0) 

`remove_entry_links` (lines 418–430) removes the target from its parents' children sets, removes the target from its children's parents sets, and then calls `self.links.remove(id)` — fully deleting the entry from `TxLinksMap`. [2](#0-1) 

The second loop (lines 261–264) then calls `remove_entry` on each id. `remove_entry` (lines 235–250) calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)` at line 242. [3](#0-2) 

`update_ancestors_index_key` (lines 432–445) calls `self.links.calc_ancestors(&child.proposal_short_id())` at line 434. [4](#0-3) 

`calc_ancestors` delegates to `calc_relative_ids` (links.rs lines 37–50), which does `self.inner.get(short_id)` — but the entry was already deleted from `self.links.inner` by the first loop. The result is `.unwrap_or_default()` → empty set. [5](#0-4) 

The ancestor loop in `update_ancestors_index_key` is therefore a no-op; no surviving ancestor ever receives `sub_descendant_weight`.

**Concrete state corruption for chain A→B→C after `remove_entry_and_descendants(B)`:**

When A was inserted, `add_descendant_weight` was called for B and C, so A's `descendants_count = 3`, `descendants_fee = fee_A + fee_B + fee_C`, etc. After removal of B and C, these fields are never decremented. A's `EvictKey` is built from these inflated values: [6](#0-5) 

`EvictKey.fee_rate` is `max(descendants_feerate, feerate)`. With inflated `descendants_fee` and `descendants_size`, the `descendants_feerate` is artificially elevated, so A sorts toward the high end of `iter_by_evict_key()` — the "never evict" end. [7](#0-6) 

`next_evict_entry` iterates `iter_by_evict_key()` ascending (lowest fee rate first), so A is skipped in favor of legitimately higher-fee transactions. [8](#0-7) 

The existing test `test_remove_entry_and_descendants` (lines 171–230) only asserts that B and C are absent from the pool and that A's descendants set is empty — it never checks A's `descendants_count`, `descendants_fee`, or `evict_key`, so the corruption goes undetected. [9](#0-8) 

## Impact Explanation
An attacker can keep low-fee transactions alive in a full mempool indefinitely. When the pool is full and `next_evict_entry` is called, A is skipped in favor of legitimately higher-fee transactions. High-fee transactions submitted by honest users are rejected or evicted while low-fee attacker transactions survive. This directly harms miner revenue and distorts the CKB fee market. This matches: **Critical — "Vulnerabilities which could easily damage CKB economy" (15001–25000 points)**.

## Likelihood Explanation
Reachable by any unprivileged transaction submitter with no special access. The attacker submits A (low fee), B spending A's output, C spending B's output, then D double-spending B's input. D's submission triggers `resolve_conflict` → `remove_entry_and_descendants(B)` → A's stats are permanently inflated. The attack is cheap (attacker pays fees for B and C which are removed), repeatable, and requires no PoW, operator access, or victim mistakes.

## Recommendation
In `remove_entry_and_descendants`, snapshot the ancestor sets for all `removed_ids` **before** the `remove_entry_links` loop, then apply `sub_descendant_weight` to surviving ancestors (those not in `removed_ids`) after removal:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();

    // Snapshot ancestor sets BEFORE clearing links
    let ancestor_map: Vec<(ProposalShortId, HashSet<ProposalShortId>)> = removed_ids
        .iter()
        .map(|rid| (rid.clone(), self.links.calc_ancestors(rid)))
        .collect();

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed_entries: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Update surviving ancestors
    for (removed_id, ancestors) in &ancestor_map {
        if let Some(removed_entry) = removed_entries.iter().find(|e| &e.proposal_short_id() == removed_id) {
            for anc_id in ancestors {
                if !removed_set.contains(anc_id) {
                    self.entries.modify_by_id(anc_id, |e| {
                        e.inner.sub_descendant_weight(removed_entry);
                        e.evict_key = e.inner.as_evict_key();
                    });
                }
            }
        }
    }

    removed_entries
}
```

## Proof of Concept
Add a unit test in `tx-pool/src/component/tests/score_key.rs` alongside `test_remove_entry_and_descendants`. Build chain A→B→C, call `pool.remove_entry_and_descendants(&b_id)`, then assert:

```rust
let a = pool.get(&a_id).unwrap();
assert_eq!(a.descendants_count, 1);   // fails: actual is 3
assert_eq!(a.descendants_fee, Capacity::shannons(100)); // fails: actual is 500
```

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

**File:** tx-pool/src/component/tests/score_key.rs (L171-230)
```rust
fn test_remove_entry_and_descendants() {
    let mut map = PoolMap::new(DEFAULT_MAX_ANCESTORS_COUNT);
    let tx1 = TxEntry::dummy_resolve(
        TransactionBuilder::default().build(),
        100,
        Capacity::shannons(100),
        100,
    );
    let tx2 = TxEntry::dummy_resolve(
        TransactionBuilder::default()
            .input(
                CellInput::new_builder()
                    .previous_output(
                        OutPoint::new_builder()
                            .tx_hash(tx1.transaction().hash())
                            .index(0u32)
                            .build(),
                    )
                    .build(),
            )
            .witness(Bytes::new())
            .build(),
        200,
        Capacity::shannons(200),
        200,
    );
    let tx3 = TxEntry::dummy_resolve(
        TransactionBuilder::default()
            .input(
                CellInput::new_builder()
                    .previous_output(
                        OutPoint::new_builder()
                            .tx_hash(tx2.transaction().hash())
                            .index(0u32)
                            .build(),
                    )
                    .build(),
            )
            .witness(Bytes::new())
            .build(),
        200,
        Capacity::shannons(200),
        200,
    );
    let tx1_id = tx1.proposal_short_id();
    let tx2_id = tx2.proposal_short_id();
    let tx3_id = tx3.proposal_short_id();
    map.add_proposed(tx1).unwrap();
    map.add_proposed(tx2).unwrap();
    map.add_proposed(tx3).unwrap();
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(descendants_set.contains(&tx2_id));
    assert!(descendants_set.contains(&tx3_id));
    map.remove_entry_and_descendants(&tx2_id);
    assert!(!map.contains_key(&tx2_id));
    assert!(!map.contains_key(&tx3_id));
    let descendants_set = map.calc_descendants(&tx1_id);
    assert!(!descendants_set.contains(&tx2_id));
    assert!(!descendants_set.contains(&tx3_id));
}
```
