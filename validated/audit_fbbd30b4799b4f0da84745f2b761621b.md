Audit Report

## Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` — (`File: tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-strips all link records via `remove_entry_links` before invoking `remove_entry` on each removed transaction. Because `update_ancestors_index_key` (called inside `remove_entry`) relies on `calc_ancestors` to discover surviving ancestors through the live link graph, and those links are already torn down, the ancestor update loop never executes. Surviving parent transactions are permanently left with inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, and therefore a stale, artificially high `evict_key`, causing them to be incorrectly deprioritized for eviction when the pool is full.

## Finding Description

`remove_entry_and_descendants` first collects the root and all descendant IDs, then calls `remove_entry_links` on every one of them before iterating to call `remove_entry`: [1](#0-0) 

`remove_entry_links` removes the target from its parents' children sets **and** deletes the target's own entry from `self.links.inner` via `self.links.remove(id)`: [2](#0-1) 

After this teardown, `remove_entry` is called for each removed ID. Inside `remove_entry`, `update_ancestors_index_key` is invoked at line 242: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())` to discover which pool entries to update: [4](#0-3) 

`calc_ancestors` delegates to `calc_relative_ids`, which does `self.inner.get(short_id)` on the links map. Since `remove_entry_links` already called `self.links.remove(id)`, the entry is absent from `self.links.inner`, so `calc_relative_ids` returns `unwrap_or_default()` — an empty set: [5](#0-4) 

The `for anc_id in &ancestors` loop body never executes. Surviving ancestors (parents of the removed root) never receive `sub_descendant_weight`, and their `evict_key` is never recomputed.

`EvictKey` is derived from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`: [6](#0-5) 

Because these fields are never decremented, the ancestor's `evict_key` permanently reflects a fee rate that includes the removed child's contribution.

## Impact Explanation

`next_evict_entry` selects the transaction to drop when the pool is full by iterating `iter_by_evict_key()` in ascending order: [7](#0-6) 

A stale, inflated `evict_key` places the ancestor higher in the ordering (less likely to be selected for eviction). When `limit_size` iterates to drop the lowest-priority transaction, the ancestor is skipped in favour of a transaction that genuinely has a lower fee rate. Legitimate higher-fee-rate transactions submitted by honest users are rejected with `Reject::Full`. The same stale key affects `check_and_record_ancestors`, which uses `iter_by_evict_key` to select which cell-dep-conflicting transactions to evict when ancestor-count limits are hit, causing the wrong transactions to be evicted there as well. An attacker can repeat the inflation cycle cheaply (submit child C at high fee rate, then conflict it with C′ at low fee rate) to keep a low-fee-rate parent permanently protected from eviction, filling the pool with otherwise-evictable transactions and causing sustained rejection of legitimate submissions — matching the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

The trigger requires only standard unprivileged RPC access (`send_transaction`). No majority hash power, no privileged role, and no social engineering is needed. The attacker pays the fee for C (the high-fee child) once per inflation cycle; C′ (the conflicting transaction) can carry a minimal fee. The inflation is permanent for the lifetime of P in the pool, and the cycle can be repeated with fresh children to keep P's `evict_key` perpetually stale. The attack is fully automated and repeatable.

## Recommendation

Before stripping link records in `remove_entry_and_descendants`, snapshot the surviving ancestors of the root transaction. After all removals are complete, call `sub_descendant_weight` and recompute `evict_key` for each surviving ancestor for each removed entry:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Snapshot surviving ancestors BEFORE tearing down links.
    let surviving_ancestors: HashSet<ProposalShortId> = self
        .links
        .calc_ancestors(id)
        .into_iter()
        .filter(|a| !removed_ids.contains(a))
        .collect();

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    let removed: Vec<TxEntry> = removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect();

    // Update surviving ancestors' descendant accounting.
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

```
State: pool is near its size limit.

1. submit_transaction(P)   // low fee rate, e.g. 1 shannon/byte
   → P.descendants_fee = P.fee
   → P.evict_key reflects fee_rate(P)

2. submit_transaction(C)   // high fee rate, spends P output
   → record_entry_descendants → update_ancestors_index_key(C, Add) fires
   → P.descendants_fee += C.fee   (inflated)
   → P.evict_key = max(descendants_feerate, feerate)  ← now high

3. submit_transaction(C')  // conflicts with C (same input)
   → resolve_conflict → remove_entry_and_descendants(C)
   → remove_entry_links called for C before remove_entry
   → calc_ancestors(C) returns ∅ (link record already deleted)
   → update_ancestors_index_key(C, Remove) loop body never executes
   → P.descendants_fee is NOT decremented  (still inflated)
   → P.evict_key is NOT recomputed         (still high)

4. Repeat steps 2–3 with fresh children to keep P.evict_key perpetually stale.

5. Pool fills up; limit_size calls next_evict_entry.
   → P is skipped because its evict_key shows a high fee rate.
   → A legitimate transaction Q with a genuinely higher fee rate
     than P is evicted instead.
   → Q's submitter receives Reject::Full.
```

Verification: add a unit test that inserts P, inserts C (child of P), removes C via `remove_entry_and_descendants`, then asserts that `pool_map.get_by_id(&P_id).unwrap().evict_key == P.as_evict_key()` (i.e., evict_key reflects only P's own fee, not C's). Without the fix, this assertion fails because `evict_key` still incorporates C's fee contribution.

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
