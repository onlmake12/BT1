### Title
`remove_entry_and_descendants` Skips Ancestor `descendants_fee/size/cycles` Decrement, Causing Stale Eviction-Priority Accounting — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all parent/child links before calling `remove_entry` on each entry. Because `update_ancestors_index_key` resolves ancestors through those same links, the call becomes a no-op for every entry in the removed subtree. Ancestor entries that remain in the pool are never told to subtract the removed descendants' `fee`, `size`, and `cycles` from their own `descendants_*` accumulators. The result is permanently inflated descendant-weight bookkeeping on surviving ancestors, corrupting the eviction-priority key (`EvictKey`) used to decide which transactions to drop when the pool is full. In the `remove_by_detached_proposal` path the same entries are immediately re-inserted, causing the ancestor's `descendants_fee/size/cycles` to be counted **twice**.

---

### Finding Description

**Root cause — `remove_entry_and_descendants`**

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← severs ALL parent↔child links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← now calls update_ancestors_index_key
        .collect()
}
``` [1](#0-0) 

`remove_entry` calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`:

```rust
// lines 235-250
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
``` [2](#0-1) 

`update_ancestors_index_key` resolves the ancestor set through `self.links.calc_ancestors(...)`:

```rust
// lines 432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // ← empty: links already gone
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
``` [3](#0-2) 

Because `remove_entry_links` was already called for every entry in `removed_ids`, `calc_ancestors` returns an empty set for each of them. The `sub_descendant_weight` call that should decrement the surviving ancestor's `descendants_fee`, `descendants_size`, and `descendants_cycles` is never reached. [4](#0-3) 

**Contrast with the single-entry path**

When `remove_entry` is called directly (not via `remove_entry_and_descendants`), links are still intact at the time `update_ancestors_index_key` runs, so ancestors are found and correctly updated. The bug is exclusive to the bulk-removal path.

**Double-counting in `remove_by_detached_proposal`**

`remove_by_detached_proposal` calls `remove_entry_and_descendants` and then immediately re-inserts every removed entry via `add_pending`:

```rust
// tx-pool/src/pool.rs  lines 343-353
let mut entries = self.pool_map.remove_entry_and_descendants(id);
entries.sort_unstable_by_key(|entry| entry.ancestors_count);
for mut entry in entries {
    entry.reset_statistic_state();
    let ret = self.add_pending(entry);   // ← calls record_entry_descendants → update_ancestors_index_key(Add)
    ...
}
``` [5](#0-4) 

`add_pending` → `add_entry` → `record_entry_descendants` → `update_ancestors_index_key(entry, EntryOp::Add)` increments the surviving ancestor's `descendants_fee/size/cycles` again. [6](#0-5) 

Because the decrement never happened, the ancestor's `descendants_fee` ends up counting the re-inserted subtree **twice**.

---

### Impact Explanation

`descendants_fee/size/cycles` feed directly into `EvictKey`:

```rust
// entry.rs  From<&TxEntry> for EvictKey
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
``` [7](#0-6) 

`next_evict_entry` selects the transaction with the lowest `EvictKey` to drop when the pool is full. An ancestor whose `descendants_fee` is artificially inflated appears to have high-value descendants and is therefore protected from eviction longer than it deserves. Consequences:

- **Pool-space squatting**: a low-fee transaction can be kept alive in a full pool by temporarily attaching high-fee children and then removing them (via RBF or conflict resolution), leaving the parent's `descendants_fee` permanently inflated.
- **Legitimate-transaction displacement**: when the pool is full, correctly-priced transactions are evicted in preference to the artificially-boosted low-fee ancestor.
- **Double-count amplification on reorg**: every chain reorganization that detaches proposals triggers `remove_by_detached_proposal`, doubling the `descendants_fee` of any pending ancestor each time the cycle repeats.

Mining priority (`AncestorsScoreSortKey`) is not affected because it uses `ancestors_fee/size/cycles`, not `descendants_*`.

---

### Likelihood Explanation

- **RBF path**: RBF is a configurable feature (`min_rbf_rate > min_fee_rate`). When enabled, any unprivileged RPC caller can submit a replacement transaction, triggering `process_rbf` → `remove_entry_and_descendants`. The attacker needs only two sequential `send_transaction` calls.
- **Reorg / detached-proposal path**: `remove_by_detached_proposal` is triggered by ordinary block processing during any chain reorganization. No special attacker action is required; the double-count accumulates passively.
- **Conflict-resolution path**: `resolve_conflict` is triggered whenever a new transaction spends an input already consumed by a pool transaction, which is a normal occurrence.

Overall likelihood: **Medium** (RBF must be enabled for the deliberate attack; the reorg path requires no attacker action at all).

---

### Recommendation

Before severing links in `remove_entry_and_descendants`, update the surviving ancestors of the root entry. One approach:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: decrement descendants_* on ancestors of the root BEFORE links are removed
    if let Some(root_entry) = self.entries.get_by_id(id).map(|e| e.inner.clone()) {
        // sum fee/size/cycles of the entire removed subtree
        let total_fee = removed_ids.iter()
            .filter_map(|rid| self.entries.get_by_id(rid))
            .fold(0u64, |acc, e| acc.saturating_add(e.inner.fee.as_u64()));
        // ... similarly for size and cycles, then call sub_descendant_weight for each ancestor
        self.update_ancestors_index_key(&root_entry, EntryOp::Remove);
        // (or accumulate a synthetic TxEntry representing the whole subtree and subtract once)
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Alternatively, track the total `descendants_fee/size/cycles` of the removed subtree and subtract it from each surviving ancestor in a single pass before any links are removed.

---

### Proof of Concept

**Scenario A — RBF-triggered inflation (requires `min_rbf_rate > min_fee_rate`)**

1. Submit `tx_A` (low fee, e.g. 100 shannons) spending a confirmed cell.
2. Submit `tx_B` (high fee, e.g. 10 000 shannons) spending `tx_A`'s output.
   - `tx_A.descendants_fee` = 10 100 shannons ✓
3. Submit `tx_C` (RBF replacement of `tx_B`, fee = 10 001 shannons, same input as `tx_B`).
   - `process_rbf` calls `remove_entry_and_descendants(tx_B_id)`.
   - Links are removed first; `update_ancestors_index_key(tx_B, Remove)` finds no ancestors → no-op.
   - `tx_A.descendants_fee` remains 10 100 shannons (should be 100 shannons).
4. Pool fills up. `next_evict_entry` iterates by `EvictKey`. `tx_A` appears to have 10 100-shannon descendants and is skipped; a legitimate higher-fee transaction is evicted instead.

**Scenario B — Reorg double-count (no attacker action needed)**

1. `tx_A` (pending), `tx_B` (proposed, child of `tx_A`).
   - `tx_A.descendants_fee` = `tx_A.fee + tx_B.fee`.
2. A 1-block reorg detaches the proposal. `remove_by_detached_proposal({tx_B})` is called.
   - `remove_entry_and_descendants(tx_B)` → links removed → ancestor decrement skipped.
   - `tx_A.descendants_fee` still = `tx_A.fee + tx_B.fee`.
3. `tx_B` is re-inserted via `add_pending` → `record_entry_descendants` → `update_ancestors_index_key(tx_B, Add)`.
   - `tx_A.descendants_fee` += `tx_B.fee` → now = `tx_A.fee + 2 × tx_B.fee`.
4. Each subsequent reorg of the same block doubles the contribution of `tx_B` in `tx_A.descendants_fee`.

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

**File:** tx-pool/src/component/pool_map.rs (L487-513)
```rust
    fn record_entry_descendants(&mut self, entry: &TxEntry) {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let outputs = entry.transaction().output_pts();
        let mut children = HashSet::new();

        // collect children
        for o in outputs {
            if let Some(ids) = self.edges.get_deps_ref(&o).cloned() {
                children.extend(ids);
            }
            if let Some(id) = self.edges.get_input_ref(&o).cloned() {
                children.insert(id);
            }
        }
        // update children
        if !children.is_empty() {
            for child in &children {
                self.links.add_parent(child, tx_short_id.clone());
            }
            if let Some(links) = self.links.inner.get_mut(&tx_short_id) {
                links.children.extend(children);
            }
            self.update_descendants_index_key(entry, EntryOp::Add);
        }
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
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

**File:** tx-pool/src/pool.rs (L343-353)
```rust
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
                    );
                }
```
