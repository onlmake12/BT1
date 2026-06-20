### Title
Ancestor Count Not Updated by Actual Removed Entries in Eviction Loop, Causing Unnecessary Tx Eviction — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `check_and_record_ancestors`, when the tx-pool evicts `cell_ref_parent` transactions to make room for a new transaction, the loop decrements `ancestors_count` by exactly 1 per eviction call, regardless of how many ancestor entries `remove_entry_and_descendants` actually removes. Because `remove_entry_and_descendants` removes the target transaction **and all its descendants**, and some of those descendants may themselves be ancestors of the incoming transaction, the actual reduction in ancestor count can be greater than 1. The loop therefore over-evicts: it continues removing legitimate pool transactions after the ancestor limit has already been satisfied.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, `check_and_record_ancestors` handles the case where a new transaction's ancestor count exceeds `max_ancestors_count` by evicting `cell_ref_parent` transactions (pool transactions referenced via cell deps): [1](#0-0) 

```rust
let mut iter = evict_candidates.iter();
while ancestors_count > self.max_ancestors_count {
    if let Some(next_id) = iter.next() {
        let removed = self.remove_entry_and_descendants(next_id);
        ancestors_count = ancestors_count.saturating_sub(1);   // ← always -1
        parents.remove(next_id);
        evicted.extend(removed);
    } else {
        break;
    }
}
```

`remove_entry_and_descendants` removes the target transaction **plus all its descendants**: [2](#0-1) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    // ...
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

`removed` can therefore contain multiple entries. If any of those descendants are also ancestors of the incoming transaction (e.g., the new tx has an input spending an output of a descendant of the cell_ref_parent), then each such removal reduces the true ancestor count by more than 1. But `ancestors_count` is only decremented by 1 per iteration.

**Concrete scenario:**

1. Pool contains tx `A` (cell_ref_parent of new tx via cell dep) and tx `B` (child of `A`, spending `A`'s output).
2. The new tx `N` has: a cell dep on `A` (making `A` a `cell_ref_parent`) **and** an input spending `B`'s output (making `B` a direct parent via input).
3. Both `A` and `B` are in the `ancestors` set; `ancestors_count` starts at `ancestors.len() + 1`.
4. The loop calls `remove_entry_and_descendants(A)`, which removes both `A` and `B` (2 ancestors of `N`).
5. `ancestors_count` is decremented by only 1, so the loop believes the count is still 1 too high.
6. The loop proceeds to evict the next `cell_ref_parent` unnecessarily.

The analog to the original report is exact: the loop traverses a chain of entries but fails to update the "current count" variable to reflect the actual state after each step, causing it to process more entries than needed and apply the wrong outcome to entries that should have been left alone.

---

### Impact Explanation

An unprivileged transaction submitter can craft a transaction that causes the tx-pool to evict more transactions than the ancestor limit strictly requires. Legitimate, fee-paying transactions belonging to other users are removed from the pool unnecessarily. Those users must resubmit their transactions, and in a congested pool they may be unable to do so. This is a targeted griefing/DoS against other pool participants: the attacker pays only the cost of submitting one transaction to displace multiple others.

---

### Likelihood Explanation

The attack is reachable via the standard `send_transaction` RPC endpoint, which is open to any unprivileged caller. The required transaction structure (a cell dep pointing to a pool tx whose descendant is also a direct input-parent of the new tx) is a valid CKB transaction and requires no special privilege. The attacker only needs to observe the current pool state to identify suitable targets.

---

### Recommendation

Replace the fixed `-1` decrement with the actual number of ancestors removed in each iteration:

```diff
 while ancestors_count > self.max_ancestors_count {
     if let Some(next_id) = iter.next() {
         let removed = self.remove_entry_and_descendants(next_id);
-        ancestors_count = ancestors_count.saturating_sub(1);
+        // Each removed entry that was an ancestor of the new tx reduces the count.
+        // Use removed.len() as the upper bound; the actual recalculation below is authoritative.
+        ancestors_count = ancestors_count.saturating_sub(removed.len());
         parents.remove(next_id);
         evicted.extend(removed);
     } else {
         break;
     }
 }
```

Because `ancestors` is recalculated from scratch after the loop anyway (line 631–633), the loop's `ancestors_count` only needs to be accurate enough to decide when to stop evicting. Using `removed.len()` as the decrement correctly reflects how many pool entries (and thus potential ancestors) were actually eliminated per step. [3](#0-2) 

---

### Proof of Concept

**Setup:**

1. Pool contains:
   - `tx_A`: a transaction with one output (index 0). `tx_A` is in the pool.
   - `tx_B`: spends `tx_A`'s output 0. `tx_B` is in the pool (child of `tx_A`).
   - 998 additional ancestor transactions forming a chain ending at `tx_B`.
   - Total ancestors reachable from `tx_B`: 1000 (at `max_ancestors_count`).

2. Attacker submits `tx_N`:
   - **cell dep** on `tx_A`'s output (making `tx_A` a `cell_ref_parent`).
   - **input** spending `tx_B`'s output (making `tx_B` a direct parent via input).
   - `ancestors` of `tx_N` = {`tx_A`, `tx_B`, ...998 more} → `ancestors_count` = 1001.

3. `ancestors_count.saturating_sub(cell_ref_parents.len())` = 1001 − 1 = 1000 ≤ 1000, so the eviction path is taken.

4. Loop iteration 1: `remove_entry_and_descendants(tx_A)` removes `tx_A` **and** `tx_B` (and potentially the entire 998-tx chain). `removed.len()` = 1000. But `ancestors_count` is decremented by only 1 → `ancestors_count` = 1000, still above the limit.

5. Loop iteration 2: the loop evicts the next `cell_ref_parent` unnecessarily, even though the true ancestor count is now 0. [4](#0-3) [2](#0-1)

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

**File:** tx-pool/src/component/pool_map.rs (L603-625)
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
