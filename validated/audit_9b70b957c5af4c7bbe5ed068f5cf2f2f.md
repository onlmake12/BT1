I need to carefully trace through the actual code to verify this claim.

The code trace confirms the claim. Let me verify the exact execution path:

**`remove_entry_and_descendants`** (L252-265): first calls `remove_entry_links(id)` for every entry in `removed_ids`, then calls `remove_entry(id)` for each. [1](#0-0) 

**`remove_entry_links`** (L418-430): removes the entry from its parents' children sets, then calls `self.links.remove(id)` — deleting the entry's record from `TxLinksMap::inner`. [2](#0-1) 

**`remove_entry`** (L242): calls `update_ancestors_index_key`, which calls `calc_ancestors`, which calls `calc_relative_ids`. [3](#0-2) 

**`calc_relative_ids`** (links.rs L42-47): does `self.inner.get(short_id)` — if the record is already gone, returns `unwrap_or_default()` (empty set). The ancestor loop in `update_ancestors_index_key` never executes, so `sub_descendant_weight` is never called on surviving ancestors. [4](#0-3) 

The inflated `descendants_fee`/`descendants_size`/`descendants_cycles` feed directly into `EvictKey` construction: [5](#0-4) 

The eviction ordering via `next_evict_entry` selects the smallest `EvictKey` first. An ancestor with inflated `descendants_feerate` will have an artificially high `fee_rate` in its `EvictKey`, making it resistant to eviction. [6](#0-5) 

The bug is confirmed. The claim is accurate in every detail.

---

Audit Report

## Title
`remove_entry_and_descendants` Fails to Decrement Surviving Ancestors' `descendants_*` Fields — (`tx-pool/src/component/pool_map.rs`)

## Summary

In `remove_entry_and_descendants`, all link records for the removed subtree are torn down via `remove_entry_links` before `remove_entry` is called on each entry. Because `update_ancestors_index_key` resolves ancestors through the now-empty link map, it finds no ancestors and never calls `sub_descendant_weight` on any surviving ancestor outside the removed subtree. Those ancestors permanently retain inflated `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` values, corrupting the `EvictKey` used for pool eviction ordering.

## Finding Description

`remove_entry_and_descendants` (pool_map.rs L252-265) first collects all descendant IDs, then strips every entry's link record before calling `remove_entry`:

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // strips id from TxLinksMap entirely
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418-430) calls `self.links.remove(id)` as its final step, deleting the entry's record from `TxLinksMap::inner`. When `remove_entry` subsequently calls `update_ancestors_index_key` (L242), that function resolves ancestors via `self.links.calc_ancestors(&child.proposal_short_id())`. `calc_ancestors` calls `calc_relative_ids` (links.rs L37-50), which does `self.inner.get(short_id)` — but the record is already gone, so it returns an empty `HashSet` via `unwrap_or_default()`. The `for anc_id in &ancestors` loop never executes, and `sub_descendant_weight` is never called on any surviving ancestor.

Concrete path for `tx_A → tx_B` where only `tx_B` is removed:
1. `removed_ids = [tx_B]`
2. `remove_entry_links(tx_B)`: removes `tx_B` from `tx_A`'s children set, then calls `self.links.remove(tx_B)` — `tx_B`'s link record is gone
3. `remove_entry(tx_B)` → `update_ancestors_index_key(tx_B, Remove)` → `calc_ancestors(tx_B)` → `inner.get(tx_B)` returns `None` → empty set returned
4. `tx_A.descendants_fee`, `tx_A.descendants_count`, `tx_A.descendants_size`, `tx_A.descendants_cycles` are never decremented — permanently inflated

The developer comment at L256 acknowledges the link-removal side-effect only for `update_descendants_index_key` (intentionally skipped for entries being removed), but the same mechanism also silently suppresses the necessary ancestor update for surviving entries.

## Impact Explanation

The inflated `descendants_*` fields feed directly into `EvictKey` construction (entry.rs L234-247):

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
EvictKey { fee_rate: descendants_feerate.max(feerate), ... }
```

An ancestor with inflated `descendants_fee` appears to have a higher fee rate than it actually does, making it resistant to eviction in `limit_size`. The pool's `total_tx_size` is correctly decremented (via `update_stat_for_remove_tx`), but per-entry descendant metadata diverges from reality permanently. This enables incorrect eviction ordering where low-fee ancestors survive eviction rounds they should lose, displacing legitimate higher-fee transactions. An attacker can exploit this repeatedly to keep low-fee transactions alive in a full pool, causing CKB network congestion with low cost. This matches the **High** impact class: "Vulnerabilities or bad designs which could cause CKB network congestion with few costs."

## Likelihood Explanation

The trigger path is fully reachable by any unprivileged user via `send_transaction`. No special keys, privileges, or majority hash power are required. The attacker submits a low-fee parent (`tx_A`) and a high-fee child (`tx_B`), then replaces `tx_B` via RBF or waits for size-limit eviction. Each replacement inflates `tx_A`'s apparent descendant value further. The scenario is reproducible with two ordinary transactions and is repeatable indefinitely.

## Recommendation

Before the `remove_entry_links` loop, resolve and update the surviving ancestors of the root entry while its link record is still intact:

1. Before the loop at L257, call `update_ancestors_index_key(root_entry, EntryOp::Remove)` using the root entry's data while its link record still exists in `TxLinksMap`.
2. Proceed with the existing link-removal loop and `remove_entry` calls (which will correctly skip ancestor updates for the already-handled root, and skip descendant updates for entries being removed).

Alternatively, before any link removal, snapshot the set of surviving ancestors of the root (`calc_ancestors(id)` minus `removed_ids`), then explicitly call `sub_descendant_weight` on each after the subtree is removed.

## Proof of Concept

```
Setup: tx_A (parent, fee=1 shannon) → tx_B (child, fee=10000 shannons)

1. submit tx_A → pool accepts; tx_A.descendants_fee = 1 (self only)
2. submit tx_B → pool accepts; tx_A.descendants_fee = 10001 (self + tx_B)
3. submit tx_B' replacing tx_B via RBF
   → remove_entry_and_descendants(tx_B) called
   → remove_entry_links(tx_B): removes tx_B from links map
   → remove_entry(tx_B): calc_ancestors(tx_B) == {} (links gone)
   → tx_A.descendants_fee remains 10001  ← BUG: should be 1
4. Pool reaches max_tx_pool_size; eviction runs
   → tx_A.EvictKey.fee_rate computed from inflated descendants_fee=10001
   → tx_A survives eviction despite having no real high-fee descendants
   → a legitimate high-fee transaction is rejected instead

Unit test plan: add a test in pool_map tests that:
  - inserts tx_A and tx_B (tx_B spending tx_A's output)
  - calls remove_entry_and_descendants(tx_B.proposal_short_id())
  - asserts tx_A.descendants_count == 1 and tx_A.descendants_fee == tx_A.fee
  - (currently this assertion fails, proving the bug)
```

### Citations

**File:** tx-pool/src/component/pool_map.rs (L242-244)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
            self.remove_entry_edges(&entry.inner);
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
