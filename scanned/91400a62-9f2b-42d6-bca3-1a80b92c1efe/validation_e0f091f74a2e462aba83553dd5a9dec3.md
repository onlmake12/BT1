### Title
Stale Ancestor `descendants_*` Fields After `remove_entry_and_descendants` Allow Tx-Pool Eviction Manipulation — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` strips all link entries before calling `remove_entry` on each removed transaction. Because `update_ancestors_index_key` relies on the live link graph to find which pool entries to update, the ancestors of the removed subtree that remain in the pool never have their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields decremented. The stale, inflated values are then baked into those entries' `EvictKey` index, making them appear more valuable than they actually are and shielding them from eviction.

---

### Finding Description

`TxEntry` carries cached aggregate statistics about its in-pool descendants: [1](#0-0) 

These fields are kept consistent through `add_descendant_weight` / `sub_descendant_weight`, which are called from `update_ancestors_index_key`: [2](#0-1) 

`update_ancestors_index_key` discovers which pool entries to update by traversing the live link graph (`calc_ancestors`). It also rewrites each ancestor's `evict_key` in the multi-index map so the sorted index stays consistent.

The problem is in `remove_entry_and_descendants`: [3](#0-2) 

The function first calls `remove_entry_links` for **every** entry in the removed set (including the root). `remove_entry_links` deletes the root's own link record and removes the root from its parents' children lists: [4](#0-3) 

Only after all links are gone does the loop call `remove_entry` for each entry. Inside `remove_entry`, `update_ancestors_index_key` calls `calc_ancestors(root_id)` — but the root's link entry no longer exists, so the traversal returns an empty set and **no ancestor outside the removed set is updated**: [5](#0-4) 

`resolve_conflict` — the code path triggered by both RBF replacement and committed-block conflict resolution — calls `remove_entry_and_descendants` directly: [6](#0-5) 

After `resolve_conflict` returns, every pool entry that was a parent of the removed subtree retains the removed children's fee/size/cycle contribution in its `descendants_*` fields and in its indexed `evict_key`.

The `EvictKey` is computed as `max(descendants_feerate, own_feerate)`: [7](#0-6) 

A stale, inflated `descendants_feerate` raises the effective `fee_rate` in the eviction index, pushing the entry toward the "do not evict" end of `iter_by_evict_key()`.

---

### Impact Explanation

When the pool reaches its size limit, `limit_size` calls `next_evict_entry`, which iterates `iter_by_evict_key()` in ascending order and evicts the entry with the lowest `fee_rate`: [8](#0-7) 

A parent transaction whose `descendants_feerate` is stale-inflated will be ranked above its true position in the eviction order. It will not be evicted even when the pool is full and its actual fee rate is below the eviction threshold. Legitimate higher-fee-rate transactions submitted by other users will be evicted in its place.

---

### Likelihood Explanation

The attack requires only two submitted transactions and one RBF replacement — all operations available to any unprivileged RPC caller (`send_transaction`). No special privilege, key material, or majority hash power is needed. The RBF path is explicitly supported and enabled when `min_rbf_rate > min_fee_rate`: [9](#0-8) 

The attacker pays fees for the child transaction but recovers the child's UTXO via the RBF replacement, so the net cost is only the incremental RBF fee. The stale state persists until the parent transaction is eventually committed or the node restarts.

---

### Recommendation

In `remove_entry_and_descendants`, update the ancestors of the root entry **before** stripping any links. Concretely:

1. Compute the set of ancestors of `id` that are **not** in the removed set.
2. For each such ancestor, call `sub_descendant_weight` for every entry being removed that is a direct or transitive descendant of that ancestor, and recompute its `evict_key`.
3. Only then proceed with `remove_entry_links` and `remove_entry` for the removed set.

Alternatively, restructure `remove_entry_and_descendants` to remove links only for intra-set edges (between entries all being removed), preserving the root-to-parent edge long enough for `update_ancestors_index_key` to traverse it correctly.

---

### Proof of Concept

1. Submit parent transaction **P** with fee rate just above `min_fee_rate` (e.g., 1 001 shannons/vByte).
2. Submit child transaction **C** spending an output of P, with a very high fee rate (e.g., 1 000 000 shannons/vByte). `add_descendant_weight` is called on P; P's `descendants_fee` and `descendants_feerate` become very large.
3. Submit replacement transaction **R** that spends the same input as C with fee ≥ `min_replace_fee(C)`. `resolve_conflict` → `remove_entry_and_descendants(C_id)` is called. Because C's link entry is stripped before `remove_entry` runs, `calc_ancestors(C_id)` returns ∅ and P's `descendants_*` fields are never decremented.
4. P now has `descendants_fee` = C's fee (stale), `descendants_count` = 2 (stale), and its indexed `evict_key.fee_rate` = stale high `descendants_feerate`.
5. Fill the pool with transactions whose actual fee rate is between P's true rate and the stale rate. `limit_size` evicts those transactions instead of P, because P's `evict_key` ranks it as more valuable.
6. P persists in the pool indefinitely, occupying space and displacing legitimate transactions, at a cost to the attacker of only the incremental RBF fee for R.

### Citations

**File:** tx-pool/src/component/entry.rs (L35-41)
```rust
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
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

**File:** tx-pool/src/pool.rs (L81-83)
```rust
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```
