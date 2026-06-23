After extensive analysis of the CKB codebase, I found a genuine analog vulnerability in the tx-pool's ancestor/descendant accounting logic.

---

### Title
Stale Descendant Fee Accounting After Batch RBF Removal Enables Pool Eviction Manipulation — (File: `tx-pool/src/component/pool_map.rs`)

### Summary
`remove_entry_and_descendants` pre-removes all link entries before calling `remove_entry` on each. Because `update_ancestors_index_key` relies on the live link graph to find ancestors, the pre-removal of links silently skips updating the `descendants_fee / descendants_size / descendants_cycles / descendants_count` fields of any in-pool parent that sits *above* the removed subtree. Those cached fields—and the `evict_key` derived from them—remain permanently inflated, letting an attacker keep a low-fee transaction in the pool longer than it deserves by exploiting the RBF path.

### Finding Description

`remove_entry_and_descendants` is the only bulk-removal path in the pool:

```rust
// tx-pool/src/component/pool_map.rs
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);          // ← strips ALL link entries first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
``` [1](#0-0) 

`remove_entry` then calls `update_ancestors_index_key`, which walks the link graph upward to find ancestors and decrement their `descendants_*` fields:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());   // ← empty after pre-removal
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),
                ...
            };
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
``` [2](#0-1) 

Because `remove_entry_links` was already called for every entry in the subtree, `calc_ancestors` returns an empty set for every entry being removed. The ancestors that remain in the pool never have `sub_descendant_weight` called on them, so their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` are permanently inflated.

The `EvictKey` is computed directly from those cached fields:

```rust
impl From<&TxEntry> for EvictKey {
    fn from(entry: &TxEntry) -> Self {
        let descendants_weight =
            get_transaction_weight(entry.descendants_size, entry.descendants_cycles);
        let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
        let feerate = FeeRate::calculate(entry.fee, weight);
        EvictKey {
            fee_rate: descendants_feerate.max(feerate),   // ← inflated
            ...
        }
    }
}
``` [3](#0-2) 

The `evict_key` stored in the sorted index is also updated inside `update_ancestors_index_key` (`e.evict_key = e.inner.as_evict_key()`), so the sorted eviction index itself is stale. [4](#0-3) 

This is called during RBF processing:

```rust
fn process_rbf(...) {
    let all_removed: Vec<_> = conflicts
        .iter()
        .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
        .collect();
    ...
}
``` [5](#0-4) 

### Impact Explanation

Any in-pool transaction that is a parent of an RBF-replaced subtree retains an inflated `descendants_feerate` in the eviction index. When the pool reaches capacity and the eviction loop runs, that parent is ranked as more valuable than it actually is and is skipped in favour of evicting legitimate high-fee transactions. A tx submitter can therefore park a near-zero-fee transaction in the pool indefinitely by repeatedly cycling high-fee children through RBF, causing legitimate transactions to be rejected with `Reject::Full`.

### Likelihood Explanation

RBF is enabled whenever `min_rbf_rate > min_fee_rate` (a common operator configuration). Any unprivileged tx-pool submitter can craft the two-step sequence below with no special access. The pool-full condition is regularly reached on mainnet during congestion, making the eviction path active.

### Recommendation

Before stripping links in `remove_entry_and_descendants`, iterate over every entry in the removed set and call `update_ancestors_index_key(entry, EntryOp::Remove)` while the link graph is still intact. This mirrors the per-entry path taken by the single-entry `remove_entry` and ensures that surviving ancestors have their `descendants_*` fields and `evict_key` correctly decremented for every removed descendant.

### Proof of Concept

1. Submit `tx_parent` with fee = 1 shannon/byte (low fee).
2. Submit `tx_child` (child of `tx_parent`) with fee = 10 000 shannons/byte (high fee).
   - `tx_parent.descendants_fee` is now incremented by `tx_child.fee` via `add_descendant_weight`.
3. Submit `tx_new` via RBF to replace `tx_child` (paying the required RBF premium). `tx_new` spends the same input as `tx_child` but does **not** depend on `tx_parent`.
   - `process_rbf` calls `remove_entry_and_descendants(tx_child_id)`.
   - Links are stripped first; `update_ancestors_index_key` finds no ancestors → `tx_parent.descendants_fee` is **not** decremented.
4. `tx_parent` now has `descendants_fee = tx_child.fee` (stale) and `descendants_count = 2` (stale), giving it an `EvictKey.fee_rate` of ~10 000 shannons/byte despite having zero real descendants.
5. When the pool fills, the eviction loop skips `tx_parent` (appears high-value) and evicts legitimate transactions instead. `tx_parent` remains in the pool indefinitely at near-zero cost after the one-time RBF payment.

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

**File:** tx-pool/src/component/entry.rs (L234-248)
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
}
```

**File:** tx-pool/src/process.rs (L203-206)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();
```
