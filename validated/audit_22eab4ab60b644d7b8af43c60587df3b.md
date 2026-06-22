### Title
Stale Descendant Statistics in `PoolMap::remove_entry_and_descendants` Corrupt Eviction Ordering - (`tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::remove_entry_and_descendants` removes all parent/child links for the entire batch **before** calling `remove_entry` on each member. Because `update_ancestors_index_key` relies on those links to walk up to ancestors, it finds an empty ancestor set and silently skips updating the `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles`, and the stored `evict_key` of every ancestor that lies **outside** the removed subtree. Those ancestors permanently carry inflated descendant statistics, corrupting the eviction order used when the tx-pool is full.

### Finding Description

`remove_entry_and_descendants` first strips all links for every entry in the batch, then iterates and calls `remove_entry` on each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);          // ← all links torn down here
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← links already gone
        .collect()
}
```

Inside `remove_entry`, the first thing done is:

```rust
// lines 242-243
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` calls `self.links.calc_ancestors(id)`, which traverses the now-empty link graph and returns an empty set. No ancestor outside the removed batch is ever reached:

```rust
// lines 432-444
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());  // empty – links gone
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),
                ...
            };
            e.evict_key = e.inner.as_evict_key();   // ← never reached for outside ancestors
        });
    }
}
```

Consider the chain **A → B → C** (A is the parent, B and C are descendants). If `remove_entry_and_descendants(B)` is called:

- B and C are removed.
- A's `descendants_count` should drop from 3 to 1, but it stays at 3.
- A's `descendants_fee`, `descendants_size`, `descendants_cycles` remain inflated.
- The stored `evict_key` for A in the `MultiIndexPoolEntryMap` is never refreshed.

The `evict_key` is a **stored, indexed field** used directly by `next_evict_entry` → `iter_by_evict_key()`:

```rust
// lines 380-385
pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
    self.entries
        .iter_by_evict_key()
        .find(move |entry| entry.status == status)
        .map(|entry| entry.id.clone())
}
```

`EvictKey` ordering prefers entries with **lower** `descendants_count` for eviction. A stale high `descendants_count` makes A appear harder to evict than it actually is, so A survives pool-full eviction rounds it should lose.

### Impact Explanation

When the tx-pool reaches its size limit (`limit_size` → `remove_entry_and_descendants`), or when a committed block invalidates a mid-chain transaction (`remove_committed_tx` → `resolve_conflict` → `remove_entry_and_descendants`), ancestors of the removed subtree retain inflated descendant statistics. The eviction comparator (`EvictKey`) uses `descendants_count` and `descendants_fee` as tiebreakers. Stale values cause:

1. **Wrong eviction victim selection**: a low-fee ancestor with stale high `descendants_count` is ranked as harder to evict than a genuinely high-descendant entry, so a different (possibly higher-fee) transaction is evicted instead.
2. **Persistent corruption**: no code path recomputes or corrects the stale `evict_key` after the fact; the corruption persists until the ancestor itself is eventually removed.

### Likelihood Explanation

The trigger is any code path that calls `remove_entry_and_descendants` on an entry that has a pool-resident ancestor. This happens routinely:

- A block commits a transaction that double-spends a mid-chain pool entry (`resolve_conflict`).
- RBF replacement evicts a mid-chain entry.
- Pool-size eviction (`limit_size`) targets a mid-chain entry.

An unprivileged tx-pool submitter can deliberately construct a parent–child chain (A → B → C), then submit a conflicting transaction that causes B and C to be evicted, leaving A with permanently stale stats. No special privilege is required beyond the ability to submit transactions.

### Recommendation

Move the link-removal step **after** the ancestor-index update, or compute the ancestor set before tearing down links. The simplest fix mirrors the single-entry `remove_entry` pattern: call `update_ancestors_index_key` while links are still intact, then remove links:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Update ancestor stats BEFORE removing links, so outside ancestors are reached.
    for id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(id) {
            self.update_ancestors_index_key(&entry.inner.clone(), EntryOp::Remove);
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry_skip_ancestor_update(id))
        .collect()
}
```

Alternatively, snapshot the ancestor set for each removed entry before any links are torn down, then apply the weight subtraction using the snapshot.

### Proof of Concept

1. Submit tx **A** (spends an on-chain UTXO, fee = 1000 shannons).
2. Submit tx **B** (spends output 0 of A, fee = 100 shannons).
3. Submit tx **C** (spends output 0 of B, fee = 100 shannons).
   - At this point A's `descendants_count = 3`, `descendants_fee = 1200`.
4. Mine a block that includes a transaction spending the same input as B (or submit a higher-fee RBF replacement for B).
   - `resolve_conflict` → `remove_entry_and_descendants(B)` removes B and C.
5. Inspect A's pool entry: `descendants_count` is still 3, `descendants_fee` is still 1200, and `evict_key` reflects those stale values.
6. Fill the pool to capacity with many small transactions. Observe that A is not evicted even though it now has zero descendants and its true `evict_key` should rank it as an eviction candidate.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** tx-pool/src/component/pool_map.rs (L380-385)
```rust
    pub(crate) fn next_evict_entry(&self, status: Status) -> Option<ProposalShortId> {
        self.entries
            .iter_by_evict_key()
            .find(move |entry| entry.status == status)
            .map(|entry| entry.id.clone())
    }
```

**File:** tx-pool/src/component/pool_map.rs (L432-444)
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
```

**File:** tx-pool/src/component/entry.rs (L132-142)
```rust
    /// Update ancestor state for remove an entry
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

**File:** tx-pool/src/component/sort_key.rs (L92-104)
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
}
```
