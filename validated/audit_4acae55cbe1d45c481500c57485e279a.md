### Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` Causes Incorrect Tx-Pool Eviction Order — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` clears all parent/child links **before** calling `remove_entry` on each node. This prevents `update_ancestors_index_key` from traversing to the removed subtree's ancestors, leaving those ancestors with stale `descendants_*` fields and a stale `evict_key` index. The eviction sort order is then incorrect: ancestors that had high-fee descendants (now removed) retain an inflated eviction priority, causing the pool to evict other transactions instead of the now-low-value ancestor.

---

### Finding Description

`PoolEntry` stores two separate indexed fields that must be kept in sync with `inner: TxEntry`:

- `score: AncestorsScoreSortKey` — used for block-template selection (sorted by ancestor fee-rate)
- `evict_key: EvictKey` — used for pool eviction (sorted by descendant fee-rate) [1](#0-0) 

When a transaction is removed via `remove_entry`, both indexes are updated correctly: [2](#0-1) 

`update_ancestors_index_key` walks the removed entry's ancestors (via `links.calc_ancestors`) and subtracts the removed entry's weight from each ancestor's `descendants_*` fields, then recomputes `evict_key`: [3](#0-2) 

However, `remove_entry_and_descendants` first strips **all** links for every node in the subtree before calling `remove_entry`: [4](#0-3) 

After `remove_entry_links(id)` runs, `id`'s own link record is gone. When `remove_entry(id)` subsequently calls `update_ancestors_index_key`, `links.calc_ancestors(&id)` returns an **empty set** because the link was already deleted. The ancestors of `id` that remain in the pool never have their `descendants_fee`, `descendants_size`, `descendants_cycles`, `descendants_count` decremented, and their `evict_key` is never recomputed.

The `evict_key` is derived from descendant fee-rate: [5](#0-4) 

A higher descendant fee-rate means a higher `evict_key`, meaning the entry is **less** likely to be evicted. After the descendants are removed, the ancestor's `evict_key` remains inflated.

This stale `evict_key` is consumed in two places:

1. **Pool eviction when full** — `next_evict_entry` iterates `iter_by_evict_key()` to find the lowest-priority entry to drop: [6](#0-5) 

2. **Ancestor-count overflow eviction** — `check_and_record_ancestors` uses `iter_by_evict_key()` to select which `cell_ref_parents` to evict: [7](#0-6) 

The stale `evict_key` is triggered whenever `remove_entry_and_descendants` is called on a subtree whose root has a parent still in the pool. This happens in `resolve_conflict` (when a new tx conflicts with an existing one) and `resolve_conflict_header_dep`: [8](#0-7) 

---

### Impact Explanation

An ancestor transaction that had high-fee descendants (now removed) retains an inflated `evict_key`. When the pool is full, `next_evict_entry` skips this ancestor (because its stale `evict_key` makes it appear high-value) and evicts a different, potentially higher-fee transaction instead. The result is:

- Low-fee transactions persist in the pool beyond their rightful lifetime.
- Legitimate high-fee transactions submitted later are rejected or evicted prematurely.
- Miner block-template revenue is degraded because the pool's eviction ordering no longer reflects actual fee-rates.
- The `check_and_record_ancestors` eviction path also uses the stale key, potentially evicting the wrong `cell_ref_parent` when ancestor limits are exceeded.

---

### Likelihood Explanation

The trigger condition — a transaction with an in-pool parent being removed via `remove_entry_and_descendants` — is a normal, frequent pool operation. Any unprivileged tx-pool submitter can deliberately construct it:

1. Submit a low-fee parent tx P.
2. Submit high-fee children C1, C2, C3 spending P's outputs (inflating P's `evict_key`).
3. Submit a double-spend of C1's input, triggering `resolve_conflict` → `remove_entry_and_descendants(C1)` (and C2, C3 if they chain). P remains with a stale, inflated `evict_key`.
4. Repeat across many parents to fill the pool with stale-key entries.
5. Subsequent legitimate high-fee transactions are evicted instead of the stale low-fee parents.

No privileged access, key material, or majority hashpower is required.

---

### Recommendation

In `remove_entry_and_descendants`, update the ancestors of the subtree root **before** clearing links, so `update_ancestors_index_key` can still traverse them:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors of the root before clearing links
    if let Some(root_entry) = self.get(id).cloned() {
        self.update_ancestors_index_key(&root_entry, EntryOp::Remove);
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

And suppress the redundant `update_ancestors_index_key` call inside `remove_entry` when called from this path (or guard it with a links-existence check).

---

### Proof of Concept

```
Pool state:
  A (fee=10, size=100) — low-fee parent
  B (fee=500, size=100) — high-fee child of A
  C (fee=500, size=100) — high-fee child of A

After adding B and C:
  A.descendants_fee = 1010, A.descendants_count = 3
  A.evict_key.fee_rate = high (reflects B+C)

Attacker submits B' (double-spend of B's input):
  resolve_conflict → remove_entry_and_descendants(B)
    remove_entry_links(B)  ← A's link to B is severed
    remove_entry(B):
      update_ancestors_index_key(B, Remove)
        calc_ancestors(B) → {} (link already gone)
        A.evict_key NOT updated  ← BUG

Pool state after:
  A.descendants_fee = 1010  (stale, should be 510 after B removed)
  A.evict_key.fee_rate = high (stale)

Pool fills up. next_evict_entry() skips A (stale high evict_key).
Legitimate tx D (fee=50) is evicted instead of A (fee=10).
``` [4](#0-3) [9](#0-8)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L46-58)
```rust
#[derive(MultiIndexMap, Clone)]
pub struct PoolEntry {
    #[multi_index(hashed_unique)]
    pub id: ProposalShortId,
    #[multi_index(ordered_non_unique)]
    pub score: AncestorsScoreSortKey,
    #[multi_index(hashed_non_unique)]
    pub status: Status,
    #[multi_index(ordered_non_unique)]
    pub evict_key: EvictKey,
    // other sort key
    pub inner: TxEntry,
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

**File:** tx-pool/src/component/pool_map.rs (L608-613)
```rust
            let evict_candidates: Vec<ProposalShortId> = self
                .entries
                .iter_by_evict_key()
                .filter(move |entry| cell_ref_parents.contains(&entry.id))
                .map(|x| x.id.clone())
                .collect();
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
