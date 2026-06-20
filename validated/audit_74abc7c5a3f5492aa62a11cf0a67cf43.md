### Title
Stale `evict_key` on External Ancestors After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

### Summary

`remove_entry_and_descendants` pre-clears all link entries before calling `remove_entry`. Because `remove_entry` → `update_ancestors_index_key` relies on `links.calc_ancestors` to find which pool entries to update, and those links are already gone, external ancestors that are **not** being removed never have their `descendants_*` fields decremented. Their `evict_key` (which is derived from `descendants_fee`/`descendants_weight`) remains permanently inflated, corrupting the eviction ordering for the lifetime of those ancestors in the pool.

---

### Finding Description

**The ordering bug in `remove_entry_and_descendants`**

`remove_entry_and_descendants` first collects `[B, C]`, then runs a pre-clearing loop:

```
for id in &removed_ids {
    self.remove_entry_links(id);   // clears B's link entry, then C's
}
```

`remove_entry_links(B)` calls `self.links.remove(B)`, which deletes B's entire entry from `TxLinksMap::inner`. [1](#0-0) 

Immediately after, `remove_entry(B)` is called, which calls `update_ancestors_index_key(B, Remove)`:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
    // ^^^ B's link entry is already gone → returns {}
    for anc_id in &ancestors { ... }   // loop body never executes
}
``` [2](#0-1) 

`calc_ancestors` walks `TxLinksMap::inner` starting from B's direct parents. Because `links.remove(B)` already deleted B's entry, `calc_relative_ids` finds nothing and returns an empty set. [3](#0-2) 

X is therefore never visited. `sub_descendant_weight` is never called on X, so `X.descendants_count`, `X.descendants_fee`, `X.descendants_size`, `X.descendants_cycles` all remain at their pre-removal values. [4](#0-3) 

**How the stale state corrupts `evict_key`**

`EvictKey` is computed as:

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
let feerate            = FeeRate::calculate(entry.fee, weight);
EvictKey {
    fee_rate: descendants_feerate.max(feerate),
    ...
    descendants_count: entry.descendants_count,
}
``` [5](#0-4) 

`fee_rate` in `EvictKey` is the **maximum** of the transaction's own fee rate and its descendants' aggregate fee rate. If B and C carried high fees, X's `descendants_feerate` was elevated. After B and C are removed without updating X, `descendants_fee` and `descendants_weight` remain inflated, so `descendants_feerate` stays high, and `EvictKey.fee_rate` stays high.

**Eviction ordering consequence**

`next_evict_entry` iterates `iter_by_evict_key()` in ascending order — the entry with the **lowest** `evict_key` is evicted first. Lower `fee_rate` → lower key → evicted sooner. [6](#0-5) 

`EvictKey::cmp` compares `fee_rate` first, then `descendants_count`, then `timestamp`. [7](#0-6) 

With X's `fee_rate` and `descendants_count` both inflated, X's `evict_key` is **higher than it should be**, so X is evicted **later** than it should be — it survives pool pressure that should have removed it.

---

### Impact Explanation

An unprivileged submitter can keep a low-fee transaction (X) alive in a full pool by:

1. Submitting X with a low fee rate.
2. Submitting high-fee descendants B → C of X (inflating X's `descendants_feerate`).
3. Submitting a double-spend of B's input, triggering `resolve_conflict` → `remove_entry_and_descendants(B)`.
4. X's `evict_key.fee_rate` now reflects the (removed) high-fee descendants, not X's actual low fee rate.
5. Under pool pressure, X is ranked as if it were a high-fee transaction and survives while genuinely high-fee transactions are rejected or evicted.

The invariant "evict_key reflects only live descendants" is broken. The pool's eviction policy is corrupted for the lifetime of X.

---

### Likelihood Explanation

The trigger path is entirely unprivileged: submit transactions via P2P/RPC, then submit a conflicting transaction. `resolve_conflict` is a standard production code path called on every new transaction admission. The attack requires no special access, no PoW, and no coordination. It is locally reproducible with a state test.

---

### Recommendation

In `remove_entry_and_descendants`, update external ancestors **before** clearing links. One correct approach: collect the set of external ancestors (those not in `removed_ids`) first, call `sub_descendant_weight` on each of them for every removed entry, then clear links and remove entries. Alternatively, restructure `remove_entry` so that `update_ancestors_index_key` is called while links are still intact, and `update_descendants_index_key` is suppressed only for entries that are themselves in the removal set.

---

### Proof of Concept

State test (no network required):

1. Insert X into pool (low fee, e.g. 1 shannon/byte).
2. Insert B as child of X (high fee, e.g. 1000 shannon/byte).
3. Insert C as child of B (high fee).
4. Assert `X.descendants_count == 3`, `X.evict_key.fee_rate` reflects descendants.
5. Call `pool_map.remove_entry_and_descendants(&B.proposal_short_id())`.
6. Assert `X` is still in pool.
7. **Bug**: `X.descendants_count` is still `3` (should be `1`); `X.evict_key.fee_rate` still reflects B+C's fees (should equal X's own low fee rate).
8. Insert a new high-fee transaction Y with no descendants; assert Y's `evict_key` is lower than X's (Y should be evicted before X under correct semantics, but with the bug X's inflated key causes Y to be evicted first).

The root cause is at: [8](#0-7) 
— links are cleared (line 257–259) before `remove_entry` (line 263) calls `update_ancestors_index_key` (line 242), which depends on those same links (line 434).

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
