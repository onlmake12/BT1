Audit Report

## Title
Stale Descendant Accounting in `remove_entry_and_descendants` Permanently Inflates Ancestor `evict_key`, Enabling Pool-Eviction Bypass — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` pre-strips all link records for every entry being removed before calling `remove_entry`. Because `remove_entry` relies on the live link graph to locate ancestors via `update_ancestors_index_key`, those ancestors are silently skipped. Every ancestor that remains in the pool retains permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, and its `evict_key` is never decremented. An unprivileged attacker can exploit this via repeated RBF to make a low-fee transaction appear to have an arbitrarily high descendant fee-rate, preventing it from ever being evicted and causing legitimate higher-fee transactions to be rejected with `Reject::Full`.

## Finding Description

`remove_entry_and_descendants` (L252–265) first collects the target and all its descendants, then strips all their link records in a first pass, then calls `remove_entry` for each: [1](#0-0) 

`remove_entry_links` (L418–430) removes the entry from its parents' children sets and removes the entry's own link node entirely: [2](#0-1) 

After `remove_entry_links(TxB)` runs, TxB's link entry is gone. When `remove_entry(TxB)` is subsequently called, it invokes `update_ancestors_index_key(&TxB, Remove)`: [3](#0-2) 

`update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, which returns an empty set because TxB's link entry was already removed: [4](#0-3) 

The `modify_by_id` loop body — which calls `sub_descendant_weight` and recomputes `evict_key` — never executes for TxA. TxA's `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` all remain at their pre-removal values: [5](#0-4) 

When the replacement TxC is subsequently added, `record_entry_descendants` → `update_ancestors_index_key(TxC, Add)` adds TxC's weight to the already-inflated TxA entry. TxA's `descendants_fee` now equals `fee_B_stale + fee_C` instead of just `fee_C`. The `evict_key` is derived from `descendants_fee`: [6](#0-5) 

The comment on L256 acknowledges only that pre-removing links prevents `update_descendants_index_key` from touching entries being removed, but it silently also disables `update_ancestors_index_key` for surviving ancestors — the actual bug.

## Impact Explanation

The `evict_key` drives pool eviction ordering. `next_evict_entry` iterates `iter_by_evict_key()` to find the lowest-priority transaction to drop when the pool is full: [7](#0-6) 

An inflated `descendants_feerate` raises the `fee_rate` field of `EvictKey`, pushing TxA toward the "do not evict" end of the ordering. A low-fee transaction with an artificially high `evict_key` survives pool-full eviction rounds that should have removed it, causing legitimate higher-fee transactions to be rejected with `Reject::Full`. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

`remove_entry_and_descendants` is called on every conflict resolution path reachable by any unprivileged RPC or P2P transaction sender: [8](#0-7) 

No special privilege is required. The attacker only needs to submit transactions via the standard P2P or RPC interface. The cost per iteration is the incremental RBF fee bump (fee_C > fee_B + ε), which is small relative to the inflation achieved. The attack is repeatable indefinitely, and each round permanently adds another stale fee delta to TxA's `descendants_fee`.

## Recommendation

Update surviving ancestors **before** stripping link records. The fix is to call `update_ancestors_index_key` for each entry being removed while the link graph is still intact, then strip links, then call `remove_entry` (which should skip the ancestor update since links are already gone):

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // 1. Update ancestors of every removed entry WHILE links are still intact.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid).map(|e| e.inner.clone()) {
            self.update_ancestors_index_key(&entry, EntryOp::Remove);
        }
    }

    // 2. Now strip links (prevents double-update inside remove_entry).
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids
        .iter()
        .filter_map(|rid| self.remove_entry(rid))
        .collect()
}
```

This ensures every ancestor that remains in the pool has its `descendants_fee/size/cycles/count` and `evict_key` correctly decremented before the link graph is torn down.

## Proof of Concept

```
Initial pool:
  TxA  fee=1, size=100   (no parents)
  TxB  fee=1000, size=100 (parent: TxA)

TxA.descendants_fee   = 1001
TxA.evict_key.fee_rate = high  (driven by TxB)

--- Attacker submits TxC (RBF of TxB, fee=1001) ---

resolve_conflict(TxB):
  remove_entry_links(TxB)   ← TxA no longer knows TxB is its child
  remove_entry(TxB):
    update_ancestors_index_key(TxB, Remove)
      calc_ancestors(TxB) = {}   ← links gone, returns empty
      → TxA.descendants_fee NOT decremented   ← BUG

add_entry(TxC):
  update_ancestors_index_key(TxC, Add)
    calc_ancestors(TxC) = {TxA}
    TxA.descendants_fee += 1001
    TxA.evict_key recomputed

Final state:
  TxA.descendants_fee = 1001 (stale TxB) + 1001 (TxC) = 2002
  Correct value:        1 (TxA) + 1001 (TxC) = 1002

Repeat N times → TxA.descendants_fee = 1 + N*1001
TxA is never evicted regardless of pool pressure.
```

A unit test can be written directly against `PoolMap` by: (1) inserting TxA and TxB with parent–child relationship, (2) calling `remove_entry_and_descendants` on TxB's id, (3) asserting that TxA's `descendants_fee` equals `TxA.fee` (not `TxA.fee + TxB.fee`), and (4) asserting that TxA's `evict_key` reflects only its own fee.

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
