### Title
Stale Parent Reference After `remove_entry_and_descendants` Causes Panic / Corrupted Pool State in Ancestor Eviction — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `check_and_record_ancestors`, when the tx-pool evicts a `cell_ref_parent` entry via `remove_entry_and_descendants`, the function removes the evicted entry **and all its descendants** from the pool and from `self.links`. However, only the directly-evicted parent is removed from the local `parents` set. If a descendant of the evicted entry is **also** a `cell_ref_parent` of the incoming transaction, it remains in `parents` as a stale reference. The subsequent `calc_relation_ids(parents, …)` call then includes that already-removed entry in the returned `ancestors` set, and `_record_ancestors` calls `get_by_id_checked` on it — a function that panics when the entry is absent — crashing the node.

---

### Finding Description

The vulnerable function is `check_and_record_ancestors` in `tx-pool/src/component/pool_map.rs`.

**Step 1 — ancestor computation:** [1](#0-0) 

`get_tx_ancenstors` returns three sets: all transitive ancestors, direct `parents`, and `cell_ref_parents` — the subset of parents that appear as cell-dep consumers of the new tx's inputs.

**Step 2 — eviction loop:** [2](#0-1) 

For each evicted candidate `next_id`, `remove_entry_and_descendants(next_id)` is called. This removes `next_id` **and every descendant** from both `self.entries` and `self.links`. [3](#0-2) 

Only `parents.remove(next_id)` is called — descendants of `next_id` that are **also** members of `parents` (because they independently appear in `cell_ref_parents`) are **not** removed from `parents`.

**Step 3 — stale ancestor re-calculation:** [4](#0-3) 

`calc_relation_ids` is called with the now-stale `parents` set. Inside `calc_relation_ids`, when a `stage` entry is not found in `self.links.inner` (because it was already removed), the function still inserts it into `relation_ids`: [5](#0-4) 

The removed descendant is therefore present in the returned `ancestors` set.

**Step 4 — panic in `_record_ancestors`:** [6](#0-5) 

`get_by_id_checked` is called for every entry in `ancestors`. Because the removed descendant is no longer in the pool, this call panics, crashing the node process.

**Secondary effect — corrupted link state:**
Even if the panic is somehow avoided, `_record_ancestors` writes the stale `parents` set (containing the removed descendant) into `self.links` for the new transaction: [7](#0-6) 

This permanently corrupts the pool's parent/child graph, causing incorrect ancestor-count and fee-rate calculations for all future descendants of the new transaction.

---

### Impact Explanation

A remote, unprivileged tx-pool submitter can crash the CKB node process (panic) or permanently corrupt the tx-pool's ancestor accounting. Corrupted accounting affects block-assembly fee ordering and the `max_ancestors_count` guard, potentially allowing transactions that should be rejected to be accepted, or causing legitimate high-fee transactions to be mis-ranked.

---

### Likelihood Explanation

The trigger requires crafting two pool transactions A and B (B a descendant of A) that both reference the same live cell output as a cell dep, plus a new transaction that spends that output, with a total ancestor chain long enough to exceed `max_ancestors_count` (default 25). All of this is achievable by any node that can submit transactions to the tx-pool — no privileged access, key material, or majority hashpower is required.

---

### Recommendation

Inside the eviction loop, after calling `remove_entry_and_descendants(next_id)`, iterate over the returned `removed` entries and remove **each** of their IDs from `parents` as well:

```rust
let removed = self.remove_entry_and_descendants(next_id);
for r in &removed {
    parents.remove(&r.proposal_short_id());
}
ancestors_count = ancestors_count.saturating_sub(removed.len());
evicted.extend(removed);
```

This ensures `parents` never contains stale references after eviction, and `ancestors_count` is decremented by the actual number of removed entries rather than always 1.

---

### Proof of Concept

1. Submit `tx_A` to the pool: it spends some confirmed cell and uses confirmed output `X` as a cell dep.
2. Submit `tx_B` to the pool: it spends an output of `tx_A` and **also** uses confirmed output `X` as a cell dep. Now `tx_B` is a descendant of `tx_A`, and both are `cell_ref_parents` candidates.
3. Build a chain of 25+ additional pool transactions rooted at `tx_A` so that `ancestors_count > max_ancestors_count`.
4. Submit `tx_new` that **spends** output `X`. `get_tx_ancenstors` returns `cell_ref_parents = {tx_A, tx_B}` and `ancestors_count > 25`.
5. The condition `ancestors_count - cell_ref_parents.len() <= 25` is satisfied, so the eviction branch is entered.
6. `remove_entry_and_descendants(tx_A)` removes both `tx_A` and `tx_B` from the pool and from `self.links`. Only `parents.remove(tx_A)` is called; `tx_B` remains in `parents`.
7. `calc_relation_ids(parents, Parents)` returns a set containing `tx_B` (stale).
8. `_record_ancestors` calls `get_by_id_checked(tx_B)` → **panic → node crash**.

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

**File:** tx-pool/src/component/pool_map.rs (L556-566)
```rust
    fn _record_ancestors(
        &mut self,
        entry: &mut TxEntry,
        ancestors: HashSet<ProposalShortId>,
        parents: HashSet<ProposalShortId>,
    ) {
        // update parents references
        for ancestor_id in &ancestors {
            let ancestor = self.get_by_id_checked(ancestor_id);
            entry.add_ancestor_weight(&ancestor.inner);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L570-579)
```rust
        for parent in &parents {
            self.links.add_child(parent, short_id.clone());
        }
        self.links.add_link(
            short_id,
            TxLinks {
                parents,
                children: Default::default(),
            },
        );
```

**File:** tx-pool/src/component/pool_map.rs (L593-595)
```rust
        let (ancestors, mut parents, cell_ref_parents) = self.get_tx_ancenstors(tx);

        let mut ancestors_count = ancestors.len() + 1;
```

**File:** tx-pool/src/component/pool_map.rs (L615-625)
```rust
            let mut iter = evict_candidates.iter();
            while ancestors_count > self.max_ancestors_count {
                if let Some(next_id) = iter.next() {
                    let removed = self.remove_entry_and_descendants(next_id);
                    ancestors_count = ancestors_count.saturating_sub(1);
                    parents.remove(next_id);
                    evicted.extend(removed);
                } else {
                    break;
                }
            }
```

**File:** tx-pool/src/component/pool_map.rs (L630-636)
```rust
        // some txs in `parents` are removed, now `ancestors` need to re-caculate,
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        // we can assume the number now is less than `max_ancestors_count`
        assert!(ancestors.len() < self.max_ancestors_count);
```

**File:** tx-pool/src/component/links.rs (L59-70)
```rust
        while let Some(id) = stage.iter().next().cloned() {
            //recursively
            if let Some(tx_links) = self.inner.get(&id) {
                for direct_id in tx_links.get_direct_ids(relation) {
                    if !relation_ids.contains(direct_id) {
                        stage.insert(direct_id.clone());
                    }
                }
            }
            stage.remove(&id);
            relation_ids.insert(id);
        }
```
