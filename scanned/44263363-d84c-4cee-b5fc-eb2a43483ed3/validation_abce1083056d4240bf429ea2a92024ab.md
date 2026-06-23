### Title
Stale `descendants_count`/`descendants_fee` on Surviving Ancestors After `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

### Summary

`remove_entry_and_descendants` pre-clears all link entries for every tx being removed **before** calling `remove_entry`. Because `update_ancestors_index_key` resolves ancestors by walking the live `links` map, it finds an empty ancestor set for every removed tx and never calls `sub_descendant_weight` on surviving ancestors. Any ancestor that is **not** in the removed subtree retains inflated `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles`, and its `evict_key` is recomputed from those stale values.

---

### Finding Description

**Step 1 — Pre-clearing loop** [1](#0-0) 

```
remove_entry_and_descendants(tx2):
  removed_ids = [tx2, tx3]
  for id in [tx2, tx3]:
      remove_entry_links(id)   ← erases link entries for tx2 AND tx3
  for id in [tx2, tx3]:
      remove_entry(id)
```

`remove_entry_links` removes the node's own entry from `links.inner` and splices it out of its neighbours' parent/child sets: [2](#0-1) 

After the loop:
- tx2's link entry → **gone**
- tx3's link entry → **gone**
- tx1's `children` set → **empty** (tx2 was spliced out)

**Step 2 — `remove_entry` calls `update_ancestors_index_key`** [3](#0-2) 

**Step 3 — `update_ancestors_index_key` walks the now-empty links** [4](#0-3) 

`calc_ancestors(tx2)` calls `calc_relative_ids` on `links.inner`, but tx2's entry was already removed, so it returns `{}`. The `for anc_id in &ancestors` loop body — which calls `sub_descendant_weight` and recomputes `evict_key` — **never executes**. tx1's counters are never decremented.

**Step 4 — Stale state in `TxEntry`** [5](#0-4) 

`sub_descendant_weight` is the only place that decrements `descendants_count`, `descendants_fee`, `descendants_size`, and `descendants_cycles`. Because it is never called for tx1, all four fields remain inflated.

**Step 5 — Stale `evict_key`**

`EvictKey` is built directly from those fields: [6](#0-5) 

`descendants_count` is a tiebreaker in `EvictKey::cmp`: [7](#0-6) 

A higher `descendants_count` produces a higher `evict_key`, so tx1 sorts **later** in the ascending eviction iterator and is evicted later than it should be.

---

### Impact Explanation

After `remove_entry_and_descendants(tx2)` with chain tx1→tx2→tx3:

| Field | Expected | Actual |
|---|---|---|
| `tx1.descendants_count` | 1 (self only) | 3 |
| `tx1.descendants_fee` | fee₁ | fee₁+fee₂+fee₃ |
| `tx1.evict_key.descendants_count` | 1 | 3 |
| `tx1.evict_key.fee_rate` | `max(feerate₁, feerate₁)` | `max(feerate₁, (fee₁+fee₂+fee₃)/weight₁₂₃)` |

Consequences:
- **Incorrect eviction ordering**: tx1 is ranked as if it still has two live descendants, so it is evicted later than lower-fee txs that correctly report `descendants_count=1`.
- **Inflated `descendants_feerate`**: if fee₂+fee₃ are high, tx1's `evict_key.fee_rate` is artificially elevated, further protecting it from eviction.
- **Persistent corruption**: the stale state persists until tx1 itself is removed; no self-healing path exists.

---

### Likelihood Explanation

The trigger is ordinary, unprivileged transaction submission. An attacker:

1. Submits tx1 (spends confirmed UTXO A).
2. Submits tx2 (spends tx1's output).
3. Submits tx3 (spends tx2's output).
4. Submits tx2′ that double-spends the same input as tx2.

Step 4 causes `resolve_conflict` → `remove_entry_and_descendants(tx2)`: [8](#0-7) 

tx1 survives with stale counters. No privileged access, no PoW, no Sybil attack required.

---

### Recommendation

Do not pre-clear links before calling `remove_entry`. Instead, let `remove_entry` handle link teardown in the correct order: first call `update_ancestors_index_key` (while links are still intact so ancestors can be found), then call `remove_entry_links`. The pre-clearing was introduced to suppress redundant `update_descendants_index_key` calls on entries that are also being removed; that can be handled by skipping the update when the descendant id is in the `removed_ids` set, rather than by destroying the link graph prematurely.

---

### Proof of Concept

```
pool = PoolMap::new(1000)
add tx1 (fee=100, size=1)
add tx2 (spends tx1.output[0], fee=100, size=1)
add tx3 (spends tx2.output[0], fee=100, size=1)

// tx1.descendants_count == 3  ✓

pool.remove_entry_and_descendants(tx2.short_id())

// tx2 and tx3 are gone; tx1 remains
assert tx1.inner.descendants_count == 1   // FAILS: actual == 3
assert tx1.inner.descendants_fee  == 100  // FAILS: actual == 300
assert tx1.evict_key.descendants_count == 1  // FAILS: actual == 3
``` [1](#0-0) [4](#0-3) [9](#0-8)

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

**File:** tx-pool/src/component/entry.rs (L40-42)
```rust
    /// descendants txs count
    pub descendants_count: usize,
    /// The unix timestamp when entering the Txpool, unit: Millisecond
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
