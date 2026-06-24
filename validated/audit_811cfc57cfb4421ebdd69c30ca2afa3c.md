Audit Report

## Title
Stale Descendant-Weight Fields on Surviving Pool Ancestors After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

## Summary

`remove_entry_and_descendants` strips all `TxLinksMap` entries for the target and its entire descendant set before calling `remove_entry` on each node. Because `update_ancestors_index_key` resolves ancestors through `self.links.calc_ancestors`, and those links are already gone by the time it is called, any pool transaction that is an ancestor of the removed subtree root but is not itself in the removed set never receives `sub_descendant_weight` calls. Its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are permanently inflated, corrupting `EvictKey` ordering for all subsequent eviction decisions on that surviving ancestor.

## Finding Description

**Root cause — `remove_entry_and_descendants` (lines 252–265):** [1](#0-0) 

The function first calls `remove_entry_links` for every node in `removed_ids`. The comment explicitly states the intent: "update links state for remove, so that we won't update_descendants_index_key in remove_entry." However, stripping links also silently disables `update_ancestors_index_key` for surviving ancestors.

**`remove_entry_links` fully removes the node from `TxLinksMap.inner` (lines 418–430):** [2](#0-1) 

**`remove_entry` calls `update_ancestors_index_key` after links are already gone (lines 242–243):** [3](#0-2) 

**`update_ancestors_index_key` resolves ancestors via `calc_ancestors` (lines 432–434):** [4](#0-3) 

**`calc_ancestors` → `calc_relative_ids` returns empty when the node is absent from `inner` (links.rs lines 42–47):** [5](#0-4) 

**Concrete scenario — chain X → A → B → C:**

`removed_ids = [A, B, C]`. Pre-strip phase calls `remove_entry_links(A)`, which removes A from X's children list and removes A's own entry from `inner`. When `remove_entry(A)` subsequently calls `update_ancestors_index_key`, `calc_ancestors` looks up A in `inner`, finds nothing, and returns an empty set. `sub_descendant_weight` is never called on X. X's `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` remain inflated by the combined weight of A, B, and C.

## Impact Explanation

`EvictKey` is computed directly from the stale descendant fields: [6](#0-5) 

`EvictKey.fee_rate = max(descendants_feerate, feerate)`. With inflated `descendants_fee` and `descendants_size`/`descendants_cycles`, X's apparent descendant fee rate is higher than its true value, so its `EvictKey` is inflated. `EvictKey` ordering evicts lowest `fee_rate` first: [7](#0-6) 

`next_evict_entry` iterates by `evict_key`: [8](#0-7) 

X survives eviction rounds it should lose. Legitimate higher-value transactions may be evicted in its place. The same stale `descendants_count` field further skews tie-breaking. This constitutes a **suboptimal implementation of the CKB state storage mechanism** (tx pool), matching the Medium (2001–10000 points) impact class. Note: the claim that `estimate_fee_rate` is affected is incorrect — that function uses `iter_by_score()` and `entry.inner.fee_rate()`, which are based on ancestor weights and individual tx fee rate, not descendant fields.

## Likelihood Explanation

The trigger is fully unprivileged. An attacker submits X → A → B → C via the standard P2P/RPC interface. Any block that includes a transaction conflicting with A's input (double-spending it) triggers `resolve_conflict` → `remove_entry_and_descendants(A)`, leaving X with permanently stale descendant weights. The attacker does not need mining power; they only need to broadcast the chain and wait for or arrange a conflicting transaction to be mined. This is a normal, reachable production code path.

## Recommendation

In `remove_entry_and_descendants`, update surviving ancestors' descendant-weight fields **before** stripping links. For each node being removed, call `update_ancestors_index_key(node, EntryOp::Remove)` while links are still intact, then strip the links. Alternatively, collect the set of surviving ancestors (those not in `removed_ids`) before any stripping and explicitly call `sub_descendant_weight` on them for each removed node. The current comment "so that we won't update_descendants_index_key in remove_entry" reveals the intent was only to skip descendant updates (since all descendants are being removed); the fix should preserve that intent while restoring ancestor updates.

## Proof of Concept

1. Build pool with chain **X → A → B → C** (each spending the previous tx's output).
2. Add all four as `Pending` entries via `pool_map.add_entry`.
3. Record `X.descendants_count` (expected: 3) and `X.descendants_fee` (expected: sum of A+B+C fees).
4. Call `pool_map.remove_entry_and_descendants(&A_id)`.
5. Assert `pool_map.contains_key(&X_id)` is `true` (X survives).
6. Assert `X.descendants_count == 1` (self only) — **this assertion fails**; the field still reads 4 (self + A + B + C).
7. Assert `X.descendants_fee == X.fee` — **this assertion fails**; the field retains the inflated sum.
8. Call `pool_map.next_evict_entry(Status::Pending)` and observe X is not selected for eviction despite having the lowest individual fee rate, because its stale `EvictKey` makes it appear more valuable than it is.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-243)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
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

**File:** tx-pool/src/component/pool_map.rs (L432-434)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
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
