### Title
Stale `parents` Set After Cascading Eviction Causes `get_by_id_checked` Panic in `_record_ancestors` — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

The question's claimed mechanism (assert at line 636 panicking due to `ancestors_count` undercounting) is **incorrect**. However, the underlying scenario — C1 and C2 both being `cell_ref_parents` of T with C2 a descendant of C1 — **does produce a real node crash** through a different code path: the `parents` local variable retains a stale reference to C2 after C2 is silently removed as a descendant of C1, and `_record_ancestors` then calls `get_by_id_checked(C2)` which panics with `"inconsistent pool"`.

---

### Finding Description

**Root cause:** In `check_and_record_ancestors`, the local `parents` set is built once by `get_tx_ancenstors` and is only partially cleaned up during the eviction loop — only the directly-evicted ID is removed via `parents.remove(next_id)`. When `remove_entry_and_descendants(C1)` also removes C2 as a cascade, C2 is purged from `self.links` and `self.entries`, but C2 remains in `parents`.

**Exact trace:**

1. **Setup:** C1 is in the pool with a cell dep on output O. C2 is a descendant of C1 (spends C1's output) and also has a cell dep on O. T consumes O as an input. `max_ancestors_count = 2`.

2. **`get_tx_ancenstors(T)`** (lines 529–553):
   - `self.edges.deps.get(&O)` returns `{C1, C2}` → both added to `cell_ref_parents` and `parents`.
   - `ancestors = {C1, C2}`, `ancestors_count = 3`. [1](#0-0) 

3. **Eviction path entered** (line 603): `3 - 2 = 1 ≤ 2`. [2](#0-1) 

4. **Eviction loop** (lines 616–625): First candidate is C1.
   - `remove_entry_and_descendants(C1)` removes **both C1 and C2** (C2 is C1's descendant).
   - `ancestors_count = 3 − 1 = 2`.
   - `parents.remove(C1)` → `parents = {C2}`. **C2 is NOT removed from `parents`.**
   - Loop exits: `2 > 2` is false. [3](#0-2) 

5. **`remove_entry_and_descendants`** (lines 252–265): removes C2 from `self.links` and `self.entries`, but the caller's local `parents` set is unaffected. [4](#0-3) 

6. **Ancestor recomputation** (lines 631–633): `calc_relation_ids({C2}, Parents)` is called. Because `calc_relation_ids` unconditionally inserts every item from `stage` into `relation_ids` regardless of whether it exists in `self.links.inner`, C2 ends up in the returned `ancestors` set even though it was removed from the pool. [5](#0-4) [6](#0-5) 

7. **Assert at line 636** does NOT panic: `ancestors.len() = 1 < max_ancestors_count = 2`. The question's specific claim about this assert is wrong. [7](#0-6) 

8. **`_record_ancestors(entry, {C2}, {C2})`** (line 638): iterates over `ancestors`, calls `get_by_id_checked(&C2)`. [8](#0-7) 

9. **`get_by_id_checked`** (line 142–144): calls `.expect("inconsistent pool")` on `None` → **thread panic, node crash**. [9](#0-8) 

---

### Impact Explanation

An unprivileged attacker can crash any CKB node by submitting three crafted transactions through the standard P2P/RPC transaction submission path. The panic unwinds the tx-pool service thread, causing a full node crash with no recovery.

---

### Likelihood Explanation

The attacker needs no special privileges. The construction (C1 with cell dep on O, C2 spending C1's output and also having a cell dep on O, T consuming O) is valid CKB transaction semantics. The only constraint is that `max_ancestors_count` must be set such that the eviction path is triggered (default is 25, so the attacker needs a chain of 26+ ancestors with at least two being cell-ref parents in the described relationship — achievable by prepending additional ancestors to C1).

---

### Recommendation

After the eviction loop, recompute `parents` by filtering out any IDs no longer present in `self.links.inner` before calling `calc_relation_ids`. Specifically, replace:

```rust
let ancestors = self.links.calc_relation_ids(parents.clone(), Relation::Parents);
```

with:

```rust
parents.retain(|id| self.links.inner.contains_key(id));
let ancestors = self.links.calc_relation_ids(parents.clone(), Relation::Parents);
```

This ensures stale entries removed as cascade descendants are not passed to `_record_ancestors`.

---

### Proof of Concept

```
1. Submit C1: inputs=[X], cell_deps=[O], outputs=[Y]   (C1 references cell O)
2. Submit C2: inputs=[Y], cell_deps=[O], outputs=[Z]   (C2 spends C1's output, also references O)
3. Submit T:  inputs=[O], ...                          (T consumes O, triggering eviction of C1+C2)
   with enough additional ancestors so ancestors_count > max_ancestors_count
   but ancestors_count - cell_ref_parents.len() <= max_ancestors_count
```

When T is submitted, `check_and_record_ancestors` evicts C1 (cascading to C2), leaves C2 in `parents`, recomputes ancestors including stale C2, then panics at `get_by_id_checked` in `_record_ancestors`.

### Citations

**File:** tx-pool/src/component/pool_map.rs (L142-144)
```rust
    fn get_by_id_checked(&self, id: &ProposalShortId) -> &PoolEntry {
        self.get_by_id(id).expect("inconsistent pool")
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

**File:** tx-pool/src/component/pool_map.rs (L529-534)
```rust
        for input in entry.inputs() {
            let input_pt = input.previous_output();
            if let Some(deps) = self.edges.deps.get(&input_pt) {
                cell_ref_parents.extend(deps.iter().cloned());
                parents.extend(deps.iter().cloned());
            }
```

**File:** tx-pool/src/component/pool_map.rs (L563-565)
```rust
        for ancestor_id in &ancestors {
            let ancestor = self.get_by_id_checked(ancestor_id);
            entry.add_ancestor_weight(&ancestor.inner);
```

**File:** tx-pool/src/component/pool_map.rs (L603-613)
```rust
        if ancestors_count.saturating_sub(cell_ref_parents.len()) <= self.max_ancestors_count {
            // if ancestors count exceed limitation,
            // try to evict some conflicted transactions due to ref cells

            // sort them to find out the transactions with lowest fees
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();
```

**File:** tx-pool/src/component/pool_map.rs (L616-625)
```rust
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

**File:** tx-pool/src/component/pool_map.rs (L631-633)
```rust
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);
```

**File:** tx-pool/src/component/pool_map.rs (L636-636)
```rust
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
