Audit Report

## Title
Premature Link Removal in `remove_entry_and_descendants` Causes Stale Descendant-Weight Statistics on Surviving Ancestor Entries - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` tears down all link-map entries for every collected id before calling `remove_entry` on any of them. Because `update_ancestors_index_key` inside `remove_entry` derives the ancestor set by querying those same links, it always receives an empty set after the pre-removal step. Surviving ancestor entries are therefore never told to decrement their descendant-weight fields, leaving permanently inflated `descendants_count / descendants_size / descendants_cycles / descendants_fee` and a stale `evict_key` on every ancestor that remains in the pool.

## Finding Description
`remove_entry_and_descendants` (L252–265) collects the target and all its descendants, then iterates `removed_ids` calling `remove_entry_links` on each one before any call to `remove_entry`:

```rust
// L257-259
for id in &removed_ids {
    self.remove_entry_links(id);   // ALL links torn down first
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` (L418–430) strips cross-references and then calls `self.links.remove(id)` (L429), deleting the entry from `TxLinksMap::inner` entirely.

When `remove_entry` (L235–250) is subsequently called, it invokes `update_ancestors_index_key` (L242):

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors = self.links.calc_ancestors(&child.proposal_short_id()); // always {}
    for anc_id in &ancestors { ... e.inner.sub_descendant_weight(child); ... }
}
```

`calc_ancestors` calls `calc_relative_ids` (links.rs L37–50), which starts with `self.inner.get(short_id)`. Since `remove_entry_links` already removed the entry from `self.links.inner`, `get` returns `None`, `direct` is empty, and the traversal returns `{}`. No ancestor is ever visited; `sub_descendant_weight` is never called; `evict_key` is never refreshed.

The inline comment at L256 (`// update links state for remove, so that we won't update_descendants_index_key in remove_entry`) confirms the intent was only to suppress redundant descendant updates, but the implementation also silently suppresses the necessary ancestor updates.

**Exploit path (minimal):**
1. Submit `tx_A` (no pool ancestors).
2. Submit `tx_B` spending an output of `tx_A`. Pool records `tx_A.descendants_count = 1`.
3. Submit `tx_C` spending the same output of `tx_A` as `tx_B` (double-spend). `resolve_conflict` (L305–332) calls `remove_entry_and_descendants(tx_B_id)`.
4. `remove_entry_links(tx_B_id)` removes `tx_B` from `self.links` and strips it from `tx_A`'s children set.
5. `remove_entry(tx_B_id)` → `update_ancestors_index_key` → `calc_ancestors(tx_B_id)` → `{}`. `tx_A.sub_descendant_weight(tx_B)` is never called.
6. `tx_A` remains in the pool with `descendants_count = 1`, inflated size/cycles/fee, and a stale `evict_key` — permanently until `tx_A` itself is removed.

No existing guard corrects this: `remove_entry` also calls `remove_entry_links(id)` at L245, but since the entry is already absent from `self.links`, this is a no-op.

## Impact Explanation
The corruption permanently distorts pool eviction ordering. `evict_key` is computed from descendant weight (L442); an ancestor with inflated descendant stats appears more valuable than it is and is evicted later than it should be, while legitimate high-fee transactions with accurate stats may be displaced first. `estimate_fee_rate` (L334+) iterates pool entries by score; stale descendant-fee values distort the fee-rate curve returned to miners and RPC callers. This constitutes a suboptimal and incorrect implementation of the CKB transaction-pool state storage mechanism, matching the **Medium (2001–10000 points)** impact class.

## Likelihood Explanation
The trigger is `resolve_conflict` (L305–332), reachable by any unprivileged peer simply by submitting a transaction that spends an output already consumed by a pool transaction. No special privilege, leaked key, or victim mistake is required. The operation is cheap (one conflicting transaction submission) and repeatable, allowing an attacker to accumulate stale metadata across many pool entries.

## Recommendation
Perform the ancestor-weight update **before** tearing down the links. While the links are still intact, call `update_ancestors_index_key` for each entry being removed, then proceed with `remove_entry_links`, and finally call `remove_entry` (which should skip the ancestor update since it was already done):

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // 1. Update surviving ancestors' stats while links are still valid
    for id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(id) {
            let inner = entry.inner.clone();
            self.update_ancestors_index_key(&inner, EntryOp::Remove);
        }
    }

    // 2. Tear down links (prevents redundant descendant updates inside remove_entry)
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

Alternatively, add a boolean parameter to `remove_entry` to skip `update_ancestors_index_key` when the caller has already handled it.

## Proof of Concept
**Manual steps:**
1. Start a CKB node with a fresh tx-pool.
2. Submit `tx_A` (spends a confirmed UTXO, no pool parents). Observe `tx_A.descendants_count == 0`.
3. Submit `tx_B` spending one output of `tx_A`. Observe `tx_A.descendants_count == 1` and `tx_A.evict_key` updated.
4. Submit `tx_C` spending the **same** output of `tx_A` as `tx_B` (double-spend / RBF). `resolve_conflict` fires and calls `remove_entry_and_descendants(tx_B_id)`.
5. Inspect `tx_A` in the pool: `descendants_count` is still `1` (should be `0`), `descendants_size / cycles / fee` are still inflated, and `evict_key` is unchanged.
6. Repeat steps 2–4 with additional child transactions to accumulate further inflation.

**Unit test plan:** Add a test in `tx-pool/src/component/pool_map.rs` that inserts a parent–child pair, calls `remove_entry_and_descendants` on the child, and asserts that the parent's `descendants_count == 0` and `evict_key` equals `parent.as_evict_key()` after removal. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
