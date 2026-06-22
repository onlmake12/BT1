### Title
Missing `descendants_*` Decrement in `remove_entry_and_descendants` Inflates Ancestor Eviction Keys — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` pre-clears all parent/child links before calling `remove_entry` on each removed transaction. Because `update_ancestors_index_key` relies on those links to find which pool-resident ancestors need their `descendants_fee / descendants_size / descendants_cycles / descendants_count` decremented, the decrement never happens. Ancestors that remain in the pool permanently carry inflated descendant statistics, corrupting their `EvictKey` and making them appear more valuable than they are. An unprivileged tx-pool submitter can exploit this to keep low-fee transactions alive in a full pool.

---

### Finding Description

**Root cause — link erasure before weight update**

`remove_entry_and_descendants` collects the target and all its descendants, then erases every link in one pass before calling `remove_entry` on each:

```
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // ← ALL links erased here, before any weight update
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← weight update attempted here
        .collect()
}
```

`remove_entry` then calls `update_ancestors_index_key`, which re-derives ancestors from the now-empty link map:

```
// lines 432-444
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← returns ∅
    for anc_id in &ancestors {                                  // ← loop never executes
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),  // ← never called
                ...
            };
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

`sub_descendant_weight` — which decrements `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` — is therefore **never called** on any pool-resident ancestor of the removed subtree.

**Contrast with single-entry removal**

`remove_entry` (used alone) calls `remove_entry_links` *after* `update_ancestors_index_key`, so the link map is still intact when ancestors are looked up. The bug is exclusive to the batch path.

**Affected callers**

Every path that removes a transaction and its descendants is affected:
- `resolve_conflict` (new tx conflicts with pool tx — most common)
- `resolve_conflict_header_dep` (header invalidated)
- `limit_size` (pool full, evict lowest-fee subtree)
- `remove_tx` (RPC `remove_transaction`)

**The existing test does not catch this**

`test_remove_entry_and_descendants` (lines 170-230) only asserts that the removed entries are absent from the pool and from the descendants set. It never checks that `tx1.descendants_fee`, `tx1.descendants_count`, etc. are decremented.

---

### Impact Explanation

After the removed subtree is gone, every pool-resident ancestor retains stale, inflated values for:

| Field | Used in |
|---|---|
| `descendants_fee` | `EvictKey::fee_rate` (via `descendants_feerate`) |
| `descendants_size` | `EvictKey::fee_rate` (via `descendants_weight`) |
| `descendants_cycles` | `EvictKey::fee_rate` (via `descendants_weight`) |
| `descendants_count` | `EvictKey::descendants_count` |

`EvictKey` determines which transaction is evicted when the pool exceeds `max_tx_pool_size`. A transaction with an inflated `descendants_feerate` or `descendants_count` is ranked as *less evictable* than it should be. Concretely:

- A low-fee ancestor that previously had a high-fee child appears to still have that high-fee child after the child is removed.
- When the pool is full, legitimate high-fee transactions may be evicted in preference to the stale low-fee ancestor.
- The ancestor persists in the pool indefinitely until it is committed or expires, occupying pool space under false pretenses.

This is a **tx-pool fairness and resource-accounting** vulnerability. It does not affect consensus (block validation does not use `descendants_*` fields), but it allows an unprivileged submitter to manipulate pool eviction ordering.

---

### Likelihood Explanation

The trigger is fully reachable by any unprivileged RPC caller:

1. Submit low-fee tx **A** via `send_transaction`.
2. Submit high-fee tx **B** spending an output of **A** (making A an ancestor of B).
3. Submit tx **C** that spends the same input as **B** (double-spend / RBF replacement).
4. `resolve_conflict` removes **B** and its descendants via `remove_entry_and_descendants`.
5. **A** remains in the pool with `descendants_fee` still including B's fee and `descendants_count` still ≥ 2.

No privileged access, no leaked keys, no majority hashpower required. The attack is repeatable and cheap.

---

### Recommendation

In `remove_entry_and_descendants`, collect the set of pool-resident ancestors of the root entry *before* erasing any links, then decrement their `descendants_*` fields explicitly for each removed entry. Alternatively, restructure the function so that `remove_entry_links` is called *after* `update_ancestors_index_key` for each entry, mirroring the single-entry `remove_entry` path. A regression test should assert that after `remove_entry_and_descendants(B)`, every remaining ancestor of B has `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles` equal to the values they would have if B had never been inserted.

---

### Proof of Concept

**Setup:** pool contains A → B → C (A is grandparent, C is grandchild).

```
add A: A.descendants_count = 1, A.descendants_fee = fee_A
add B (child of A): A.descendants_count = 2, A.descendants_fee = fee_A + fee_B
add C (child of B): A.descendants_count = 3, A.descendants_fee = fee_A + fee_B + fee_C
```

**Trigger:** call `remove_entry_and_descendants(B)`.

```
removed_ids = [B, C]
remove_entry_links(B)  // B's link to A erased
remove_entry_links(C)  // C's link to B erased

remove_entry(B):
  update_ancestors_index_key(B, Remove)
    calc_ancestors(B) → ∅  // link already gone
    // A.sub_descendant_weight(B) NEVER CALLED
remove_entry(C):
  update_ancestors_index_key(C, Remove)
    calc_ancestors(C) → ∅  // link already gone
    // A.sub_descendant_weight(C) NEVER CALLED
```

**Result:** A remains in pool with `descendants_count = 3` and `descendants_fee = fee_A + fee_B + fee_C`, even though B and C are gone. A's `EvictKey` is computed from these stale values, making A appear more valuable than it is and shielding it from eviction. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/component/pool_map.rs (L432-444)
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
