### Title
Stale `evict_key` on Ancestor Entries After `remove_entry_and_descendants` — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all parent/child links for the entire removed subtree are torn down **before** `remove_entry` is called on each entry. Because `update_ancestors_index_key` resolves ancestors through those same links, the ancestors of the root entry (which remain in the pool) never have their `descendants_*` fields or `evict_key` updated. This leaves surviving ancestor entries with stale, inflated eviction keys, corrupting the pool's eviction ordering.

---

### Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1 — strip all links:** [1](#0-0) 

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← removes ALL parent/child links for every entry
    }
    ...
}
```

**Phase 2 — remove each entry:** [2](#0-1) 

Inside `remove_entry`, the first thing called is: [3](#0-2) 

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` resolves ancestors by calling `self.links.calc_ancestors(...)`: [4](#0-3) 

Because Phase 1 already called `remove_entry_links` on the root `id`, the link from the root to its parents is gone. `calc_ancestors` therefore returns an **empty set**, and the loop body never executes. The ancestors' `descendants_count`, `descendants_size`, `descendants_cycles`, `descendants_fee`, and `evict_key` are **never decremented**.

The comment in Phase 1 acknowledges the intent is to skip `update_descendants_index_key` (correct — descendants are all being removed). But it silently also skips `update_ancestors_index_key`, which is incorrect because the ancestors remain in the pool.

The `evict_key` is a multi-index sort key on every `PoolEntry`: [5](#0-4) 

It is computed from `descendants_fee` and `descendants_size/cycles`: [6](#0-5) 

After the subtree is removed, the ancestor's `descendants_fee` still reflects the removed children, making the ancestor's `evict_key` appear higher (more valuable) than it actually is.

---

### Impact Explanation

`evict_key` drives `next_evict_entry`, which is the sole mechanism used by `limit_size` to select which transaction to drop when the pool exceeds `max_tx_pool_size`: [7](#0-6) 

`next_evict_entry` iterates entries in ascending `evict_key` order and picks the first match: [8](#0-7) 

A stale, inflated `evict_key` on an ancestor means it is ranked as harder to evict than it deserves. When the pool is full, `limit_size` will skip over this ancestor and instead evict a genuinely higher-fee transaction. The result is:

- A low-fee ancestor transaction survives pool pressure it should not survive.
- Legitimate high-fee transactions submitted by other users are rejected with `Reject::Full`.

`remove_entry_and_descendants` is called from multiple reachable paths:
- `limit_size` itself (recursive eviction loop)
- `remove_by_detached_proposal` (reorg handling)
- `resolve_conflict_header_dep` (header dep invalidation)
- `check_and_record_ancestors` (cell-ref ancestor eviction)
- `remove_tx` (explicit removal) [9](#0-8) 

---

### Likelihood Explanation

Any unprivileged `send_transaction` RPC caller can reach this path:

1. Submit parent tx `P` with a low fee rate.
2. Submit child txs `C1…Cn` with high fee rates (boosting `P`'s `descendants_fee` and `evict_key`).
3. Trigger removal of `C1…Cn` — e.g., via RBF replacement (submitting a conflicting tx that spends the same inputs as `C1…Cn`), or by waiting for a reorg that detaches the proposal window.
4. `P`'s `evict_key` is now stale/inflated.
5. Fill the pool with many small transactions to trigger `limit_size`.
6. `P` is not evicted; other users' high-fee transactions are rejected instead.

RBF is enabled when `min_rbf_rate > min_fee_rate` (a common node configuration), making step 3 straightforward. [10](#0-9) 

---

### Recommendation

Before stripping links in `remove_entry_and_descendants`, explicitly update the ancestors of the root entry. Concretely, before the link-removal loop, call `update_ancestors_index_key` for the root entry only (not for descendants, since their ancestors are also being removed):

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // NEW: update ancestors of the root entry BEFORE links are torn down
    if let Some(root_entry) = self.entries.get_by_id(id) {
        self.update_ancestors_index_key(&root_entry.inner.clone(), EntryOp::Remove);
    }

    // strip links so that remove_entry won't re-run update_descendants_index_key
    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

This mirrors the pattern used in the single-entry `remove_entry` path, which correctly calls both `update_ancestors_index_key` and `update_descendants_index_key` before removing links. [3](#0-2) 

---

### Proof of Concept

**Setup:** Pool with three entries in a chain: `tx1 → tx2 → tx3` (tx2 spends tx1's output, tx3 spends tx2's output). All have `fee=100, size=100`.

After insertion, `tx1.descendants_fee = 300` (self + tx2 + tx3), so `tx1.evict_key` reflects a high descendants fee rate.

**Trigger:** Call `pool_map.remove_entry_and_descendants(&tx2_id)` (removes tx2 and tx3).

**Expected:** `tx1.descendants_fee` drops to `100` (only itself), `tx1.evict_key` is recomputed to reflect its actual lone fee rate.

**Actual:** `tx1.descendants_fee` remains `300`, `tx1.evict_key` is unchanged. `tx1` is ranked as if it still has two high-fee descendants.

**Consequence:** When `limit_size` runs and the pool is full, `tx1` is not selected for eviction even though it has the lowest actual fee rate among all pending entries. A different, genuinely higher-fee transaction is evicted instead.

The existing test `test_remove_entry` in `tx-pool/src/component/tests/score_key.rs` verifies `ancestors_count` is updated after `remove_entry` (single-entry path), but there is **no corresponding test** verifying that `descendants_*` fields on surviving ancestors are correctly updated after `remove_entry_and_descendants`. [11](#0-10) [4](#0-3)

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

**File:** tx-pool/src/pool.rs (L80-83)
```rust
    /// Check whether tx-pool enable RBF
    pub fn enable_rbf(&self) -> bool {
        self.config.min_rbf_rate > self.config.min_fee_rate
    }
```

**File:** tx-pool/src/pool.rs (L253-268)
```rust
    fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
        let short_id = tx.proposal_short_id();
        if let Some(_entry) = self.pool_map.remove_entry(&short_id) {
            debug!("remove_committed_tx for {}", tx.hash());
        }
        {
            for (entry, reject) in self.pool_map.resolve_conflict(tx) {
                debug!(
                    "removed {} for committed: {}",
                    entry.transaction().hash(),
                    tx.hash()
                );
                callbacks.call_reject(self, &entry, reject);
            }
        }
    }
```

**File:** tx-pool/src/pool.rs (L292-329)
```rust
    pub(crate) fn limit_size(
        &mut self,
        callbacks: &Callbacks,
        current_entry_id: Option<&ProposalShortId>,
    ) -> Option<Reject> {
        let mut ret = None;
        while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
            let next_evict_entry = || {
                self.pool_map
                    .next_evict_entry(Status::Pending)
                    .or_else(|| self.pool_map.next_evict_entry(Status::Gap))
                    .or_else(|| self.pool_map.next_evict_entry(Status::Proposed))
            };

            if let Some(id) = next_evict_entry() {
                let removed = self.pool_map.remove_entry_and_descendants(&id);
                for entry in removed {
                    let tx_hash = entry.transaction().hash();
                    debug!(
                        "Removed by size limit {} timestamp({})",
                        tx_hash, entry.timestamp
                    );
                    let reject = Reject::Full(format!(
                        "the fee_rate for this transaction is: {}",
                        entry.fee_rate()
                    ));
                    if let Some(short_id) = current_entry_id
                        && entry.proposal_short_id() == *short_id
                    {
                        ret = Some(reject.clone());
                    }
                    callbacks.call_reject(self, &entry, reject);
                }
            }
        }
        self.pool_map.entries.shrink_to_fit();
        ret
    }
```
