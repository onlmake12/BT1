I have the full code. Let me trace the exact execution path step by step.

I have all the code needed for a complete, line-by-line trace. Let me deliver the verdict.

---

### Title
Stale `descendants_*` / `evict_key` on surviving ancestors after `remove_entry_and_descendants` — (`tx-pool/src/component/pool_map.rs`)

### Summary

`remove_entry_and_descendants` pre-removes every link for the target subtree **before** calling `remove_entry` on each node. Because `remove_entry` calls `update_ancestors_index_key`, which re-derives ancestors from the now-empty `links` map, any ancestor that lives **outside** the removed subtree never has its `descendants_count / descendants_fee / descendants_size / descendants_cycles` decremented. Its `evict_key` is permanently inflated for as long as it stays in the pool.

### Finding Description

**Exact execution trace for chain tx0 → tx1 → tx2, calling `remove_entry_and_descendants(tx1)`:**

**Phase 1 — pre-remove all links** (`pool_map.rs` lines 257-259):

```
remove_entry_links(tx1):
  links.remove_child(&tx0, &tx1)   // tx0.children = {}
  links.remove_parent(&tx2, &tx1)  // tx2.parents = {}
  links.remove(tx1)                // tx1 gone from links map

remove_entry_links(tx2):
  links.remove(tx2)                // tx2 gone from links map
```

After Phase 1, tx0 is still in the pool and in `links`, but its `children` set is empty. tx1 and tx2 are gone from `links`. [1](#0-0) 

**Phase 2 — `remove_entry(tx1)`** (line 242 calls `update_ancestors_index_key`):

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
    // ^^^ tx1 is gone from links.inner → calc_relative_ids returns {} immediately
    for anc_id in &ancestors { ... }   // loop body never executes
}
``` [2](#0-1) 

`calc_relative_ids` looks up `self.inner.get(&tx1_id)` which returns `None` because tx1 was removed in Phase 1, so `direct` is the empty set and the BFS returns `{}`. [3](#0-2) 

**Result:** tx0 remains in the pool with stale fields:

| Field | Expected | Actual |
|---|---|---|
| `descendants_count` | 1 | 3 |
| `descendants_fee` | fee(tx0) | fee(tx0)+fee(tx1)+fee(tx2) |
| `descendants_size` | size(tx0) | size(tx0)+size(tx1)+size(tx2) |
| `descendants_cycles` | cycles(tx0) | cycles(tx0)+cycles(tx1)+cycles(tx2) |

The `evict_key` stored in the multi-index map is computed from these fields and is never refreshed: [4](#0-3) 

The comment on line 256 reveals the intent — skip `update_descendants_index_key` for the removed set — but the implementation also silences `update_ancestors_index_key` for surviving ancestors, which is the bug: [5](#0-4) 

### Impact Explanation

`EvictKey` ordering is: lowest `fee_rate` first, then highest `descendants_count` first, then latest `timestamp` first. [6](#0-5) 

`fee_rate` inside `EvictKey` is `descendants_feerate.max(feerate)`. With phantom descendants, `descendants_feerate` is computed over a larger fee/weight than reality, inflating the `fee_rate` field. A higher `fee_rate` means the entry sorts **later** in eviction order — it is evicted less aggressively than it should be. [4](#0-3) 

`next_evict_entry` picks the first entry from `iter_by_evict_key()`, i.e., the one with the smallest `evict_key`. With tx0's key inflated, it is skipped during eviction even when it should be the next candidate. [7](#0-6) 

**Concrete pool-pinning scenario:**
1. Attacker submits many low-fee tx0-class transactions, each with a descendant chain (tx1, tx2).
2. Attacker double-spends each tx1 with a tx1′ that spends the same input. `resolve_conflict` calls `remove_entry_and_descendants(tx1_id)` for each.
3. Each surviving tx0 now has an inflated `evict_key`. The pool is full of low-fee transactions that appear more valuable than they are.
4. Legitimate high-fee transactions cannot enter because eviction selects the wrong (or no) victim. [8](#0-7) 

### Likelihood Explanation

The trigger path is entirely unprivileged: submit a transaction that double-spends an in-pool child. This is a standard P2P/RPC transaction submission path. No PoW, no key material, no operator access is required. The attacker pays fees for the initial chain and the double-spend, but the cost is proportional to the number of tx0 entries they want to pin, not to the pool size.

### Recommendation

In `remove_entry_and_descendants`, update ancestors **before** removing links. Specifically, for each entry being removed, call `update_ancestors_index_key(entry, Remove)` while the links are still intact, then remove the links. Alternatively, after the pre-removal loop, explicitly walk the set of surviving ancestors (those in `removed_ids`'s parent set but not in `removed_ids` themselves) and call `sub_descendant_weight` on each for every removed descendant.

### Proof of Concept

Invariant test (pseudocode):

```rust
// Setup: tx0 -> tx1 -> tx2
map.add_pending(tx0); map.add_pending(tx1); map.add_pending(tx2);

// Trigger: remove tx1 and its descendants
map.remove_entry_and_descendants(&tx1_id);

// tx0 must still be in pool
assert!(map.contains_key(&tx0_id));

// Invariant: descendants_count of tx0 must equal actual live descendants
let live_desc = map.calc_descendants(&tx0_id).len() + 1; // +1 for self
let stored = map.get(&tx0_id).unwrap().descendants_count;
assert_eq!(stored, live_desc);
// FAILS: stored == 3, live_desc == 1
``` [1](#0-0) [2](#0-1)

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
