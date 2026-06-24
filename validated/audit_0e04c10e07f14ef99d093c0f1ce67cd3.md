The code confirms the claim. Let me verify the links implementation to complete the analysis.

Audit Report

## Title
Stale `descendants_*` stats after `remove_entry_and_descendants` corrupts eviction ordering — (File: `tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` clears all parent/child links for every entry in the removal set before calling `remove_entry` on each. Because `update_ancestors_index_key` resolves ancestors through those same (now-cleared) links, it finds an empty ancestor set and never decrements the `descendants_fee / descendants_size / descendants_cycles / descendants_count` fields of entries that are true ancestors of the evicted subtree. Those ancestors retain permanently inflated `descendants_*` stats, which raises their `evict_key.fee_rate` above reality and makes them artificially resistant to eviction. Any unprivileged submitter can exploit this via a cheap RBF-style flash-child attack to keep low-fee transactions in the pool while legitimate higher-fee transactions are evicted instead.

## Finding Description

`remove_entry_and_descendants` collects the target and all its in-pool descendants, then proceeds in two phases:

```rust
// Phase 1 – clear all links first
for id in &removed_ids {
    self.remove_entry_links(id);   // strips parents[] and children[] for every removed entry
}
// Phase 2 – remove each entry
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
``` [1](#0-0) 

`remove_entry_links` removes the entry from its parents' `children` sets, removes the entry from its children's `parents` sets, and deletes the entry's own link record entirely: [2](#0-1) 

`remove_entry` then calls `update_ancestors_index_key`: [3](#0-2) 

`update_ancestors_index_key` resolves the ancestor set through `self.links.calc_ancestors(...)`: [4](#0-3) 

`calc_ancestors` walks the `parents` field of the entry's link record: [5](#0-4) 

Because Phase 1 already called `remove_entry_links` on child C, C's link record is gone and its `parents` set is empty. `calc_ancestors(C)` therefore returns `{}`. The loop in `update_ancestors_index_key` never executes, so `sub_descendant_weight` is never called on parent P. P's `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain at their inflated post-add values indefinitely.

The `EvictKey` for P is derived directly from those stale fields: [6](#0-5) 

`fee_rate = max(descendants_feerate, own_feerate)`. With inflated `descendants_fee` and `descendants_size`, `descendants_feerate` is higher than reality, so P's `evict_key.fee_rate` is higher than reality.

`next_evict_entry` selects the entry with the **lowest** `evict_key` for eviction: [7](#0-6) 

P is therefore skipped in favour of entries with genuinely higher (but correctly computed) fee rates.

The comment in `remove_entry_and_descendants` states the intent is to suppress `update_descendants_index_key` (since all descendants are being removed anyway). That intent is correct, but the implementation also silences `update_ancestors_index_key`, which is the bug — ancestors of the removed subtree are **not** being removed and must have their stats decremented. [8](#0-7) 

## Impact Explanation

When the tx-pool is near capacity, the eviction loop repeatedly calls `next_evict_entry` and removes the lowest-`evict_key` entry. A parent P whose `descendants_*` stats are stale appears to have a higher `descendants_feerate` than it actually does, so it is consistently skipped. Legitimate transactions from other users — with correctly computed, genuinely higher fee rates — are evicted instead. The distortion persists until P is confirmed in a block, expires, or is otherwise removed. An attacker who repeats the attack across many parent UTXOs can fill the pool with low-fee transactions that all carry inflated eviction keys, effectively blocking legitimate transactions from the pool. This matches the allowed impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" — High (10001–15000 points)**.

## Likelihood Explanation

The attack is reachable by any unprivileged `send_transaction` RPC caller or P2P relay peer. No special role, leaked key, or majority hash power is required. The incremental cost per iteration is `C′.fee − C.fee` (the RBF premium), which can be made arbitrarily small by choosing C with a tiny absolute fee but a high fee-per-byte ratio. The attack is repeatable as long as the attacker controls fresh UTXOs to serve as the conflicting input X. `resolve_conflict` is also triggered by non-RBF double-spend attempts and by `resolve_conflict_header_dep`, widening the trigger surface. [9](#0-8) 

## Recommendation

Decrement ancestors' `descendants_*` stats **before** clearing links in `remove_entry_and_descendants`, while the link graph is still intact:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // 1. Update ancestors' descendants_* stats while links are still intact.
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid).map(|e| e.inner.clone()) {
            self.update_ancestors_index_key(&entry, EntryOp::Remove);
        }
    }

    // 2. Clear links so that remove_entry skips the (already-done) ancestor update
    //    and also skips the descendant update (all descendants are being removed).
    for rid in &removed_ids {
        self.remove_entry_links(rid);
    }

    removed_ids.iter().filter_map(|rid| self.remove_entry(rid)).collect()
}
```

After step 1, `update_ancestors_index_key` inside `remove_entry` will find an empty ancestor set (links cleared) and become a no-op, which is the correct and intended behaviour for the descendant direction. The ancestor direction is already handled correctly in step 1.

## Proof of Concept

**Manual steps (RBF enabled, pool near capacity):**

1. Submit parent tx **P** — fee 1 shannon, size 200 B → `fee_rate ≈ 0.005 sh/B`. P would normally be the first eviction candidate.
2. Submit child tx **C** — spends P's output **and** attacker-controlled UTXO X; fee 10 000 shannons, size 200 B.
   - After `add_entry(C)`: `P.descendants_fee = 10 001 sh`, `P.descendants_size = 400 B`, `P.evict_key.fee_rate ≈ 25 sh/B`.
3. Submit conflicting tx **C′** — spends UTXO X only; fee rate 51 sh/B (meets RBF rules).
   - `resolve_conflict` → `remove_entry_and_descendants(C)`.
   - `remove_entry_links(C)` clears C↔P link.
   - `remove_entry(C)` → `update_ancestors_index_key(C, Remove)` → `calc_ancestors(C) == {}` → no-op.
   - **P.descendants_fee still = 10 001 sh** (stale). **P.evict_key.fee_rate still ≈ 25 sh/B** (stale).
4. Pool is now full. Any honest user tx with actual fee rate between 0.005 and 25 sh/B is evicted before P.
5. Repeat with fresh UTXOs to accumulate multiple stale-stats entries.

**Unit test sketch:**

```rust
#[test]
fn test_stale_descendants_after_remove_entry_and_descendants() {
    let mut pool = PoolMap::new(100);
    // add P (low fee)
    // add C (child of P, high fee, also spends attacker UTXO X)
    // assert P.descendants_fee == P.fee + C.fee
    // call pool.remove_entry_and_descendants(C.short_id())
    // assert P.descendants_fee == P.fee  // FAILS before fix, passes after
    // assert P.evict_key == P.as_evict_key()  // FAILS before fix
}
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

**File:** tx-pool/src/component/links.rs (L78-80)
```rust
    pub fn calc_ancestors(&self, short_id: &ProposalShortId) -> HashSet<ProposalShortId> {
        self.calc_relative_ids(short_id, Relation::Parents)
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
