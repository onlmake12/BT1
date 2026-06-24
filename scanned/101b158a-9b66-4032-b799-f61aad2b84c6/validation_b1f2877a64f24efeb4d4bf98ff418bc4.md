Audit Report

## Title
`descendants_*` Fields Permanently Stale After `remove_entry_and_descendants` Due to Pre-Removal of Links — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `PoolMap::remove_entry_and_descendants`, all link entries for the removed subtree are torn down in a first pass before `remove_entry` is called on each node. Because `update_ancestors_index_key` discovers ancestors by looking up the child's own link entry (which has already been erased), it finds an empty ancestor set and never calls `sub_descendant_weight` on surviving ancestors. The `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` fields of every surviving ancestor are permanently inflated, corrupting eviction-key ordering for the remainder of those entries' lifetimes.

## Finding Description

`remove_entry_and_descendants` (pool_map.rs L252–265) operates in two sequential passes:

**Pass 1** — for every id in `removed_ids`, call `remove_entry_links(id)`: [1](#0-0) 

`remove_entry_links` (L418–430) removes the id from its parents' children sets, removes the id from its children's parents sets, and then calls `self.links.remove(id)`, which deletes the link entry from `TxLinksMap::inner` entirely. [2](#0-1) 

**Pass 2** — for every id, call `remove_entry(id)`: [3](#0-2) 

Inside `remove_entry`, the first thing called is `update_ancestors_index_key`: [4](#0-3) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`: [5](#0-4) 

`calc_ancestors` calls `calc_relative_ids`, which does `self.inner.get(short_id)`. Since the link entry was already deleted in Pass 1, this returns `None`, and `.unwrap_or_default()` yields an empty set: [6](#0-5) 

The ancestor loop in `update_ancestors_index_key` therefore iterates over zero entries — `sub_descendant_weight` is never called on any surviving ancestor. The `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` fields of every ancestor that remains in the pool are permanently inflated. [7](#0-6) 

The comment at L256 acknowledges the pre-removal is intentional to suppress `update_descendants_index_key` (correct — descendants are being removed anyway), but it inadvertently also suppresses `update_ancestors_index_key` for surviving ancestors.

There is no periodic recomputation of per-entry `descendants_*` fields; `recompute_total_stat` only recomputes pool-wide `total_tx_size`/`total_tx_cycles`: [8](#0-7) 

## Impact Explanation

The `EvictKey` for each entry is computed from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`: [9](#0-8) 

Entries with a higher `descendants_feerate` are evicted **later**. An ancestor whose `descendants_fee` is inflated (because removed high-fee descendants were never subtracted) appears to have a higher feerate than it actually does, so it is deprioritised for eviction. This allows low-fee transactions to occupy pool space beyond their fair share and resist eviction.

An attacker can exploit this repeatedly at low cost to fill the pool with low-fee transactions that cannot be properly evicted, preventing legitimate higher-fee transactions from entering the pool. This maps to **High impact: Vulnerabilities or bad designs which could cause CKB network congestion with few costs** (10001–15000 points).

Additionally, `to_info()` exposes `descendants_size` and `descendants_cycles` directly to RPC callers: [10](#0-9) 

Wallets, fee estimators, and block assemblers receive permanently incorrect descendant statistics.

## Likelihood Explanation

`remove_entry_and_descendants` is reachable from multiple unprivileged paths: `resolve_conflict` (double-spend/RBF), `limit_size` (pool full), `resolve_conflict_header_dep` (reorg), and `remove_by_detached_proposal` (proposal expiry): [11](#0-10) 

Any unprivileged submitter can trigger the bug with three transactions and one conflicting submission. The attack is cheap, deterministic, and repeatable. Each invocation permanently inflates one or more ancestor entries' evict keys for the remainder of their pool lifetime.

## Recommendation

Before erasing links in `remove_entry_and_descendants`, collect and update surviving ancestors' descendant accounting while the link graph is still intact:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update surviving ancestors BEFORE links are torn down.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid) {
            let inner = entry.inner.clone();
            self.update_ancestors_index_key(&inner, EntryOp::Remove);
        }
    }

    // Now safe to remove links (descendants need not be updated since they are all being removed).
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids
        .iter()
        .filter_map(|rid| self.remove_entry_without_ancestor_update(rid))
        .collect()
}
```

Alternatively, add a flag to `remove_entry` to skip `update_ancestors_index_key`, and call the ancestor update explicitly before link removal.

## Proof of Concept

**Setup:** Submit A (low fee, no parents), B (child of A), C (grandchild of A via B). After insertion:
- `A.descendants_count = 3`, `A.descendants_fee = fee_A + fee_B + fee_C`

**Trigger:** Submit a transaction D that spends the same input as B. `resolve_conflict` calls `remove_entry_and_descendants(B_id)`.

**Execution trace:**
1. `removed_ids = [B_id, C_id]`
2. Pass 1: `remove_entry_links(B_id)` — removes B's link entry from `TxLinksMap::inner`; `remove_entry_links(C_id)` — removes C's link entry
3. Pass 2: `remove_entry(B_id)` → `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B_id)` → `self.inner.get(B_id)` returns `None` → empty ancestor set → A's `descendants_*` not touched
4. `remove_entry(C_id)` → same result

**After removal:**
```
A.descendants_count  = 3   // should be 1
A.descendants_fee    = fee_A + fee_B + fee_C  // should be fee_A
A.descendants_size   = size_A + size_B + size_C  // should be size_A
A.descendants_cycles = cycles_A + cycles_B + cycles_C  // should be cycles_A
```

A's `EvictKey` reflects a falsely high `descendants_feerate`, preventing correct eviction ordering for the remainder of A's time in the pool. Repeating this attack with many ancestors fills the pool with entries that resist eviction.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-242)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

**File:** tx-pool/src/component/pool_map.rs (L256-259)
```rust
        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L261-264)
```rust
        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
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

**File:** tx-pool/src/component/pool_map.rs (L432-434)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
```

**File:** tx-pool/src/component/pool_map.rs (L435-444)
```rust
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
```

**File:** tx-pool/src/component/pool_map.rs (L698-708)
```rust
    fn recompute_total_stat(&self) -> Option<(usize, Cycle)> {
        self.entries.iter().try_fold(
            (0usize, 0 as Cycle),
            |(total_size, total_cycles), (_, entry)| {
                Some((
                    total_size.checked_add(entry.inner.size)?,
                    total_cycles.checked_add(entry.inner.cycles)?,
                ))
            },
        )
    }
```

**File:** tx-pool/src/component/links.rs (L42-47)
```rust
        let direct = self
            .inner
            .get(short_id)
            .map(|link| link.get_direct_ids(relation))
            .cloned()
            .unwrap_or_default();
```

**File:** tx-pool/src/component/entry.rs (L182-194)
```rust
    pub fn to_info(&self) -> TxEntryInfo {
        TxEntryInfo {
            cycles: self.cycles,
            size: self.size as u64,
            fee: self.fee,
            ancestors_size: self.ancestors_size as u64,
            ancestors_cycles: self.ancestors_cycles,
            descendants_size: self.descendants_size as u64,
            descendants_cycles: self.descendants_cycles,
            ancestors_count: self.ancestors_count as u64,
            timestamp: self.timestamp,
        }
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
