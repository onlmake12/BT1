Audit Report

## Title
Asymmetric Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Inflation of Ancestor `EvictKey` - (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`PoolMap::remove_entry_and_descendants` erases all link records for the removed subtree before invoking `remove_entry` on each entry. Because `update_ancestors_index_key` resolves ancestors via `self.links.calc_ancestors`, and those links are already gone by the time it is called, `sub_descendant_weight` is never applied to surviving ancestors. The add path correctly increments ancestor descendant weights, but the remove path never decrements them, leaving permanently inflated `descendants_fee`/`descendants_size`/`descendants_cycles`/`descendants_count` on any ancestor that survives the removal. An unprivileged attacker can exploit this via repeated child-submit / conflict-submit cycles to make a low-fee parent transaction eviction-resistant, enabling pool-slot squatting at near-zero cost.

## Finding Description

**Add path (correct):**

`add_entry` calls `record_entry_descendants`, which ends with `update_ancestors_index_key(entry, EntryOp::Add)`. At that point `self.links` still contains the full parent chain, so `calc_ancestors` returns all ancestors and `add_descendant_weight` is called on each. [1](#0-0) 

**Remove path (broken):**

`remove_entry_and_descendants` first calls `remove_entry_links` for every entry in the subtree, then calls `remove_entry` for each: [2](#0-1) 

`remove_entry_links` calls `self.links.remove(id)`, which deletes the entry from `self.links.inner`: [3](#0-2) [4](#0-3) 

After that, `remove_entry` calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`: [5](#0-4) 

`update_ancestors_index_key` resolves ancestors by calling `self.links.calc_ancestors(&child.proposal_short_id())`. Because the link entry was already removed, `calc_relative_ids` finds no entry in `self.links.inner` for the child and returns an empty set via `unwrap_or_default()`: [6](#0-5) 

Therefore `sub_descendant_weight` is never called on any surviving ancestor: [7](#0-6) 

The comment in `remove_entry_and_descendants` ("update links state for remove, so that we won't update_descendants_index_key in remove_entry") shows the intent was only to suppress the `update_descendants_index_key` call (which would try to update entries that are themselves being removed). The optimization inadvertently also suppresses the `update_ancestors_index_key` call for surviving ancestors.

**Exploit trigger — `resolve_conflict`:**

Every time a submitted transaction spends an output already claimed by a pool transaction, `resolve_conflict` calls `remove_entry_and_descendants` on the conflicting entry: [8](#0-7) 

This is reachable by any unprivileged user via the `send_transaction` RPC.

## Impact Explanation

The stale `descendants_fee` / `descendants_size` / `descendants_cycles` fields feed directly into `EvictKey` computation: [9](#0-8) 

An inflated `descendants_fee` raises `descendants_feerate`, which raises `EvictKey.fee_rate`. The eviction iterator picks the entry with the **lowest** `EvictKey`: [10](#0-9) 

`EvictKey` ordering compares `fee_rate` first: [11](#0-10) 

A parent with inflated `descendants_fee` is pushed to the back of the eviction queue and becomes effectively immune to eviction. An attacker with enough UTXOs can fill the pool with eviction-resistant low-fee transactions, blocking legitimate transactions from entering. This matches the **High (10001–15000 points)** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs*.

## Likelihood Explanation

The attack requires only the ability to call `send_transaction` RPC, which is available to any unprivileged user. The attacker needs one confirmed UTXO per parent transaction they wish to make eviction-resistant. The cost per pool slot is the minimum fee (as low as 1 shannon). The child and conflict transactions are never confirmed (they perpetually conflict with each other), so the attacker's UTXOs are not consumed beyond the parent's fee. The cycle can be executed in a tight loop with no special privileges, keys, or hashpower.

## Recommendation

Before erasing link records in `remove_entry_and_descendants`, update surviving ancestors while links are still intact. Concretely:

1. Collect the set of entries being removed (`removed_ids`).
2. For each entry in `removed_ids` whose **direct parents are not also in `removed_ids`** (i.e., the entry has surviving ancestors), call `update_ancestors_index_key(entry, EntryOp::Remove)` **before** any `remove_entry_links` call.
3. Only then proceed with the existing link-teardown and `remove_entry` loop.

Alternatively, pass a pre-computed ancestor set into `remove_entry` so that link teardown order does not affect ancestor accounting.

## Proof of Concept

**Setup:** `tx_parent` in pool with `fee = 1 shannon`, `size = 100`. Initial state: `tx_parent.descendants_fee = 1`, `tx_parent.descendants_count = 1`.

**Cycle (repeat N times):**

1. Submit `tx_child_N` spending `tx_parent`'s output, `fee = 1000 shannons`.
   - `add_entry` → `update_ancestors_index_key(tx_child_N, Add)` → `tx_parent.descendants_fee += 1000`.

2. Submit `tx_conflict_N` spending the same output as `tx_child_N`.
   - `resolve_conflict` → `remove_entry_and_descendants(tx_child_N)` → `remove_entry_links` erases links → `update_ancestors_index_key(tx_child_N, Remove)` finds empty ancestor set → `tx_parent.descendants_fee` **not decremented**.
   - `tx_conflict_N` added as new child of `tx_parent` → `tx_parent.descendants_fee += tx_conflict_N.fee`.

**After N cycles:**
```
tx_parent.descendants_fee  ≈ 1 + N × 1000   (correct value: ≤ 1001)
tx_parent.EvictKey.fee_rate ≈ N × 10 shannon/byte  (correct: ~0.01)
```

A unit test can assert this invariant: after `remove_entry_and_descendants`, every surviving ancestor's `descendants_fee` must equal the sum of fees of its actual remaining descendants. The bug is confirmed when this assertion fails after one conflict cycle.

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

**File:** tx-pool/src/component/pool_map.rs (L511-512)
```rust
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
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

**File:** tx-pool/src/component/links.rs (L94-96)
```rust
    pub fn remove(&mut self, short_id: &ProposalShortId) -> Option<TxLinks> {
        self.inner.remove(short_id)
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
