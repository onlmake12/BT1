Audit Report

## Title
Stale `EvictKey` on Surviving Ancestors After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-clears all link entries for the entire removed subtree before calling `remove_entry` on each node. Because `remove_entry` calls `update_ancestors_index_key`, which resolves ancestors via the now-cleared links, it finds an empty ancestor set and performs no updates. Surviving ancestors of the removed root permanently retain inflated `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles`, and a correspondingly stale `evict_key`, enabling an attacker to shield low-fee transactions from eviction indefinitely.

## Finding Description

**Root cause:** `remove_entry_and_descendants` (lines 252–265) first collects `removed_ids = [root] + calc_descendants(root)`, then iterates over all of them calling `remove_entry_links`. [1](#0-0) 

`remove_entry_links` (lines 418–430) does three things for each removed node: removes the node from its parents' children sets, removes the node from its children's parents sets, and removes the node's own entry from `self.links.inner`. [2](#0-1) 

For the concrete chain `ancestor_tx → tx_root → child_tx`, when `remove_entry_links(tx_root_id)` runs, it calls `self.links.remove_child(&ancestor_tx_id, &tx_root_id)` (removing `tx_root` from `ancestor_tx`'s children) and then `self.links.remove(tx_root_id)` (removing `tx_root`'s own entry from `self.links.inner`). After the pre-clearing loop, `tx_root` no longer exists in `self.links.inner`. [3](#0-2) 

When `remove_entry(tx_root_id)` is subsequently called, it invokes `update_ancestors_index_key(&tx_root_entry, EntryOp::Remove)`. [4](#0-3) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`. [5](#0-4) 

`calc_ancestors` delegates to `calc_relative_ids`, which calls `self.inner.get(short_id)`. Since `tx_root` was already removed from `self.links.inner`, this returns `None`, and `unwrap_or_default()` yields an empty `HashSet`. [6](#0-5) 

The loop in `update_ancestors_index_key` never executes. `sub_descendant_weight` is never called on `ancestor_tx`, and `e.evict_key = e.inner.as_evict_key()` is never refreshed. [7](#0-6) 

`ancestor_tx` permanently retains inflated `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles`. [8](#0-7) 

`EvictKey` is computed from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`, so the stale fields produce an inflated `fee_rate` and `descendants_count` in the evict key. [9](#0-8) 

`EvictKey` ordering places the lowest `fee_rate` first (ascending). The stale inflated `fee_rate` pushes `ancestor_tx` toward the back of the eviction queue, so `next_evict_entry` never selects it. [10](#0-9) 

The comment at line 256 (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) reveals the intent: pre-clearing was meant only to suppress redundant `update_descendants_index_key` calls on nodes that are themselves being removed. The unintended side effect is that `update_ancestors_index_key` is also silenced for surviving ancestors. [11](#0-10) 

**Exploit path:**
1. Submit `ancestor_tx` with a low fee rate.
2. Submit `tx_root` + `child_tx` (high fee rate, spending `ancestor_tx`'s output). `ancestor_tx.evict_key` is boosted by the high-fee descendants via `update_ancestors_index_key` during insertion. [12](#0-11) 
3. Submit `conflict_tx` spending the same input as `tx_root`. This triggers `resolve_conflict`, which calls `remove_entry_and_descendants(&tx_root_id)`. [13](#0-12) 
4. Due to the bug, `ancestor_tx.evict_key` retains the inflated `fee_rate` and `descendants_count` for the rest of its lifetime in the pool.
5. `next_evict_entry` iterates `iter_by_evict_key()` in ascending order; `ancestor_tx` is never selected for eviction despite its true low fee rate. [14](#0-13) 

## Impact Explanation

A low-fee transaction can be shielded from eviction indefinitely by exploiting the stale `evict_key`. An attacker can repeat this pattern across multiple UTXOs to fill the mempool with low-fee transactions that are never evicted, blocking legitimate higher-fee transactions from entering the pool. The high-fee chain (`tx_root` + descendants) is never confirmed on-chain since it is removed from the pool by the conflict, so the attacker pays only the fees for `ancestor_tx` and `conflict_tx` per round. This matches the High impact: **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs"** (10001–15000 points). [14](#0-13) 

## Likelihood Explanation

The attack requires only standard P2P transaction relay — no privileged access, no PoW, no key material. The attacker submits two sets of ordinary valid transactions (the descendant chain and the conflict). The pool size limit bounds the descendant chain length but does not prevent the attack. The exploit is repeatable: each round costs only the fees for `ancestor_tx` and `conflict_tx`, while the high-fee chain is never confirmed. Multiple attackers or a single attacker with multiple UTXOs can saturate the pool. [1](#0-0) 

## Recommendation

In `remove_entry_and_descendants`, capture the ancestor set of the root node **before** any link removal, then apply `sub_descendant_weight` and refresh `evict_key` on each surviving ancestor for every node in the removed subtree. Concretely: before the `for id in &removed_ids { self.remove_entry_links(id); }` loop, call `self.links.calc_ancestors(id)` to capture surviving ancestors, then after removal iterate over that set and call `sub_descendant_weight` / `as_evict_key()` for each removed entry. Alternatively, restructure `remove_entry_and_descendants` to call `update_ancestors_index_key` for the root while links are still intact, before pre-clearing links. The pre-clearing optimization for `update_descendants_index_key` can be preserved by only clearing the links of the non-root nodes in the subtree after the ancestor update. [1](#0-0) 

## Proof of Concept

Construct a three-node chain (`ancestor_tx → tx_root → child_tx`) in a `PoolMap`, then call `remove_entry_and_descendants(&tx_root_id)`, and assert the following invariant on `ancestor_tx`:

```rust
let entry = pool_map.get(&ancestor_tx_id).unwrap();
assert_eq!(
    entry.descendants_count,
    1 + pool_map.calc_descendants(&ancestor_tx_id).len()
);
```

This assertion fails because `entry.descendants_count` still includes `tx_root` and `child_tx` (inflated by 2), while `calc_descendants` (operating on the now-correct links) returns an empty set (true count = 0, so expected value = 1). Similarly, asserting `entry.as_evict_key() == pool_map.get_by_id(&ancestor_tx_id).unwrap().evict_key` will fail because the stored `evict_key` was never refreshed. The bug is deterministically reproducible with this minimal unit test. [1](#0-0)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-242)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
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

**File:** tx-pool/src/component/pool_map.rs (L487-513)
```rust
    fn record_entry_descendants(&mut self, entry: &TxEntry) {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let outputs = entry.transaction().output_pts();
        let mut children = HashSet::new();

        // collect children
        for o in outputs {
            if let Some(ids) = self.edges.get_deps_ref(&o).cloned() {
                children.extend(ids);
            }
            if let Some(id) = self.edges.get_input_ref(&o).cloned() {
                children.insert(id);
            }
        }
        // update children
        if !children.is_empty() {
            for child in &children {
                self.links.add_parent(child, tx_short_id.clone());
            }
            if let Some(links) = self.links.inner.get_mut(&tx_short_id) {
                links.children.extend(children);
            }
            self.update_descendants_index_key(entry, EntryOp::Add);
        }
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
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

**File:** tx-pool/src/component/entry.rs (L132-142)
```rust
    /// Update ancestor state for remove an entry
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
