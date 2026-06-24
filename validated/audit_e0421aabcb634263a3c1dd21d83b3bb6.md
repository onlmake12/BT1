Audit Report

## Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Keys — (File: tx-pool/src/component/pool_map.rs)

## Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link entries from `self.links` before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`, and those links are already gone, the call returns an empty set. Ancestor entries that remain in the pool are never updated: their `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count`, and derived `evict_key` permanently reflect the removed descendants, corrupting eviction ordering in `limit_size` and allowing low-fee ancestors to block eviction while legitimate high-fee transactions are rejected with `Reject::Full`.

## Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1** (L257–259) iterates every entry in the subtree and calls `remove_entry_links(id)` for each:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);
}
```

`remove_entry_links` (L418–430) removes `id` from its parents' children sets, from its children's parents sets, and then calls `self.links.remove(id)`, fully erasing the entry from `TxLinksMap`. [1](#0-0) 

**Phase 2** (L261–264) calls `remove_entry(id)` for each removed entry. Inside `remove_entry` (L235–250), the first call is `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`. [2](#0-1) 

`update_ancestors_index_key` (L432–445) resolves ancestors via `self.links.calc_ancestors(&child.proposal_short_id())`. [3](#0-2) 

`calc_ancestors` calls `calc_relative_ids` (links.rs L37–50), which looks up the entry in `self.links.inner`. Since Phase 1 already called `self.links.remove(id)` for every entry in the subtree, the lookup returns `None`, `direct` is empty, and `calc_ancestors` returns an empty `HashSet`. [4](#0-3) 

The loop body that calls `e.inner.sub_descendant_weight(child)` and updates `e.evict_key` is never reached for any ancestor that remains in the pool. The comment in the source (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) confirms the pre-removal is intentional to skip updating descendants (which are being removed anyway), but it inadvertently also disables the ancestor update path. [5](#0-4) 

The existing test `test_remove_entry_and_descendants` (score_key.rs L170–230) only asserts that tx2 and tx3 are absent and that tx1's descendants link set is empty. It never checks `tx1.descendants_count`, `tx1.descendants_fee`, or `tx1.evict_key`, so the stale accounting is not caught. [6](#0-5) 

## Impact Explanation

`limit_size` (pool.rs L292–329) evicts entries by calling `next_evict_entry`, which iterates entries ordered by `evict_key`. [7](#0-6) 

`EvictKey` is computed as `fee_rate: descendants_feerate.max(feerate)` (entry.rs L243). An ancestor whose high-fee descendants were removed still carries their fee contribution in `descendants_fee`, so `descendants_feerate` is inflated, `evict_key.fee_rate` is inflated, and the entry sorts as more valuable than it actually is — it is skipped during eviction. [8](#0-7) 

The pool fills with stale-accounting low-fee ancestors, causing legitimate high-fee transactions to be rejected with `Reject::Full`. This matches: **High (10001–15000 points) — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

`remove_entry_and_descendants` is called from multiple reachable paths. The most accessible to an unprivileged attacker is `check_and_record_ancestors` (pool_map.rs L588–639): when a submitted transaction exceeds `max_ancestors_count` and has `cell_ref_parents`, the code evicts those parents via `remove_entry_and_descendants`. [9](#0-8) 

An attacker can craft a transaction chain (tx_A low-fee parent → tx_B high-fee child that is also a cell dep) and then submit a new transaction that triggers eviction of tx_B, leaving tx_A with stale inflated accounting. This requires no mining power and can be repeated cheaply to saturate the pool with stale-accounting entries.

## Recommendation

Before pre-removing links in `remove_entry_and_descendants`, collect the set of external ancestors — ancestors of the root entry that are not themselves in `removed_ids` — and call `sub_descendant_weight` + update `evict_key` on each of them for every entry being removed. Alternatively, restructure the function to call `update_ancestors_index_key` for the root entry before any `remove_entry_links` calls (since descendants' ancestor fields need not be updated — they are being removed). The comment's stated intent (skip `update_descendants_index_key` for entries being removed) can still be achieved by a targeted guard rather than blanket pre-removal of all links.

## Proof of Concept

```
Setup:
  tx_A: fee=1 shannon, size=100  (no parents)
  tx_B: fee=1000 shannons, size=100  (child of tx_A)

After add_entry(tx_A) then add_entry(tx_B):
  tx_A.descendants_fee   = 1001 shannons
  tx_A.descendants_count = 2
  tx_A.evict_key.fee_rate ≈ 1001/200  (high)

Trigger (unprivileged path):
  Submit tx_C that has tx_B as a cell_dep and exceeds max_ancestors_count.
  → check_and_record_ancestors evicts tx_B via remove_entry_and_descendants(tx_B.id)

After removal:
  tx_B is gone.
  tx_A.descendants_fee   = 1001 shannons  ← STALE (should be 1)
  tx_A.descendants_count = 2              ← STALE (should be 1)
  tx_A.evict_key.fee_rate ≈ 1001/200      ← STALE (should be 1/100)

Repeat to fill pool with stale-accounting tx_A entries.
limit_size() calls next_evict_entry() — tx_A entries are skipped.
A legitimate tx with fee=500 shannons is rejected with Reject::Full.
```

A unit test can confirm this by asserting `tx1.descendants_count == 1` and `tx1.descendants_fee == 1 shannon` after calling `map.remove_entry_and_descendants(&tx2_id)` in the existing `test_remove_entry_and_descendants` fixture — this assertion will fail against the current code.

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

**File:** tx-pool/src/component/pool_map.rs (L603-625)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

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

**File:** tx-pool/src/component/tests/score_key.rs (L170-230)
```rust
#[test]
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
