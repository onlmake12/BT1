### Title
Ancestors' Descendant-Weight Fields Never Decremented After Batch Removal in `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link records for every entry in the batch *before* calling `remove_entry` on each one. Because `update_ancestors_index_key` resolves ancestors by looking up the link record of the entry being removed, and that record is already gone, the lookup always returns an empty set. As a result, the `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` fields of every pool-resident ancestor of the removed batch are **never decremented**. Those ancestors permanently carry inflated descendant-weight values, corrupting the `EvictKey` used to select which transactions to evict when the pool is full.

---

### Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1 — pre-remove all links:**
```rust
// tx-pool/src/component/pool_map.rs  L252-259
for id in &removed_ids {
    self.remove_entry_links(id);   // deletes self.links.inner[id]
}
```

`remove_entry_links` calls `self.links.remove(id)`, which deletes the entry's record from `TxLinksMap::inner`.

**Phase 2 — remove each entry:**
```rust
removed_ids.iter()
    .filter_map(|id| self.remove_entry(id))
    .collect()
```

Inside `remove_entry`:
```rust
// L242
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` calls:
```rust
// L433-434
let ancestors: HashSet<ProposalShortId> =
    self.links.calc_ancestors(&child.proposal_short_id());
```

`calc_ancestors` → `calc_relative_ids` → `self.inner.get(short_id)`. Because Phase 1 already deleted `short_id` from `self.links.inner`, this lookup returns `None`, and `calc_ancestors` returns an **empty set**. The loop over ancestors never executes, so `sub_descendant_weight` is never called on any pool-resident ancestor.

The comment in the code acknowledges only half the effect: *"update links state for remove, so that we won't `update_descendants_index_key` in `remove_entry`"*. The intent was to avoid double-counting descendant removals, but the same pre-removal also silently disables `update_ancestors_index_key`, which is the bug.

The `EvictKey` for every ancestor that survives the batch removal is now computed from stale, inflated values:

```rust
// tx-pool/src/component/entry.rs  L234-247
EvictKey {
    fee_rate: descendants_feerate.max(feerate),
    timestamp: entry.timestamp,
    descendants_count: entry.descendants_count,   // ← never decremented
}
```

`next_evict_entry` iterates `iter_by_evict_key()` in ascending order; a higher `descendants_count` and inflated `descendants_feerate` push the ancestor toward the *non-evict* end of the ordering.

---

### Impact Explanation

An attacker who controls the removed descendants can permanently inflate the `EvictKey` of any ancestor transaction they choose. In a full pool (`total_tx_size > max_tx_pool_size`), `limit_size` calls `remove_entry_and_descendants` on the entry returned by `next_evict_entry`. Because the ancestor's `EvictKey` is inflated, it is never selected for eviction even when its true fee rate is the lowest in the pool. This allows a low-fee transaction to occupy pool space indefinitely, blocking admission of legitimate higher-fee transactions submitted by other users.

The inflation is **cumulative and unbounded**: each round of submitting-and-conflicting descendants adds another layer of phantom descendant weight to the ancestor. The `saturating_add` in `add_descendant_weight` means the values can reach `u64::MAX` / `usize::MAX` without wrapping, and `saturating_sub` in `sub_descendant_weight` is never reached to correct them.

---

### Likelihood Explanation

The trigger path requires only standard, unprivileged RPC calls:

1. Submit transaction **A** (low fee) via `send_transaction`.
2. Submit transaction **B** (spends an output of A) via `send_transaction`.
3. Submit transaction **C** (spends an output of B) via `send_transaction`.
4. Submit transaction **B′** (double-spends B's input, higher fee to pass RBF rules) via `send_transaction`.
5. `resolve_conflict` / `process_rbf` calls `remove_entry_and_descendants(B)`, removing B and C. A's `descendants_count` remains 3 instead of 1; `descendants_fee/size/cycles` remain inflated.
6. Repeat from step 2 to further inflate A's descendant weight.

No privileged access, no majority hash power, and no Sybil attack is required. The attack is cheap: the attacker only needs to pay the fee for B′ each round (B and C fees are refunded in the sense that those UTXOs are never spent on-chain).

---

### Recommendation

The pre-removal of links must not happen before `update_ancestors_index_key` has had a chance to walk those links. Two concrete fixes:

1. **Compute and apply ancestor updates before removing links.** In `remove_entry_and_descendants`, for each entry in `removed_ids`, call `update_ancestors_index_key(entry, EntryOp::Remove)` while the link records are still intact, then remove the links.

2. **Restrict the pre-removal optimization to descendants only.** The comment's stated goal is to suppress `update_descendants_index_key` for entries that are themselves being removed. A narrower fix is to remove only the *children* pointers (not the *parents* pointers) before the loop, so that `calc_descendants` returns empty (suppressing the unwanted descendant update) while `calc_ancestors` still works correctly for the ancestor update.

A strict interface analogous to the Orchid report's long-term suggestion would make it impossible to remove a link record without simultaneously applying the corresponding weight adjustment to all affected ancestors.

---

### Proof of Concept

Given pool state: **A → B → C** (A is parent of B, B is parent of C).

After `add_entry` for all three, A's `descendants_count = 3`, `descendants_size = size_A + size_B + size_C`.

Call `remove_entry_and_descendants(B)`:

- Phase 1: `remove_entry_links(B)` deletes `links.inner[B]`; `remove_entry_links(C)` deletes `links.inner[C]`.
- Phase 2, processing B: `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B)` → `links.inner.get(B)` → `None` → returns `{}`. **A's `sub_descendant_weight(B)` is never called.**
- Phase 2, processing C: same — `calc_ancestors(C)` → `None` → `{}`. **A's `sub_descendant_weight(C)` is never called.**

After the call, A remains in the pool with `descendants_count = 3`, `descendants_size = size_A + size_B + size_C`, `descendants_fee = fee_A + fee_B + fee_C` — all values that should have been reduced to `descendants_count = 1`, `descendants_size = size_A`, `descendants_fee = fee_A`.

The existing test `test_remove_entry_and_descendants` only asserts that B and C are absent from the pool and absent from A's *descendant set* (via `calc_descendants`), but does **not** assert that A's `descendants_count` or `descendants_fee` fields were correctly decremented — confirming the bug is untested. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

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

**File:** tx-pool/src/component/pool_map.rs (L418-445)
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
