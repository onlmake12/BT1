### Title
Stale `EvictKey` and Descendant Statistics Corruption in `remove_entry_and_descendants` — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` in the CKB tx-pool first strips all link entries for the entire removed subtree, then calls `remove_entry` for each node. `remove_entry` relies on those same links to find ancestors and update their descendant-weight statistics and `evict_key`. Because the links are already gone, `calc_ancestors` returns an empty set, the ancestor update is silently skipped, and every ancestor that sits **outside** the removed subtree is left with permanently inflated `descendants_count`, `descendants_fee`, `descendants_size`, `descendants_cycles`, and a stale `evict_key`. This is the direct CKB analog of the Diamond library's remove-before-add corruption: a secondary data structure (the per-entry descendant statistics and eviction key) is corrupted by an out-of-order teardown.

---

### Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1 – strip all links** [1](#0-0) 

```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // removes id's own TxLinks entry
}
```

`remove_entry_links` calls `self.links.remove(id)`, which deletes the node's entry from `TxLinksMap::inner`. [2](#0-1) 

**Phase 2 – remove each entry** [3](#0-2) 

Inside `remove_entry`, the first thing called is `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`: [4](#0-3) 

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
    // ancestors is EMPTY — link entry was already removed in Phase 1
    for anc_id in &ancestors { ... }   // loop body never executes
}
```

`calc_ancestors` calls `calc_relative_ids`, which does `self.inner.get(short_id)` — returning `None` because Phase 1 already called `self.links.remove(id)`. [5](#0-4) 

Consequently, for every ancestor **A** that is *not* in the removed subtree, the following fields in `A.inner` are never decremented: [6](#0-5) 

- `descendants_count`
- `descendants_size`
- `descendants_cycles`
- `descendants_fee`

And the `evict_key` stored in `PoolEntry` for A is never recomputed: [7](#0-6) 

```rust
self.entries.modify_by_id(anc_id, |e| {
    e.inner.sub_descendant_weight(child);
    e.evict_key = e.inner.as_evict_key();   // ← never reached
});
```

`EvictKey` is built from `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`: [8](#0-7) 

```rust
let descendants_feerate = FeeRate::calculate(entry.descendants_fee, descendants_weight);
let feerate = FeeRate::calculate(entry.fee, weight);
EvictKey {
    fee_rate: descendants_feerate.max(feerate),
    descendants_count: entry.descendants_count,
    ...
}
```

`EvictKey` ordering: lower `fee_rate` → evicted first; at equal `fee_rate`, lower `descendants_count` → evicted first. [9](#0-8) 

With inflated `descendants_count` and `descendants_fee`, A's `EvictKey` is **artificially elevated**, making A harder to evict than it should be.

---

### Impact Explanation

`next_evict_entry` selects the entry with the smallest `evict_key` for removal: [10](#0-9) 

`limit_size` calls `next_evict_entry` in a loop until the pool is within its size budget: [11](#0-10) 

Because A's `evict_key` is inflated, A is skipped during eviction even when it should be the first candidate. Other, legitimately higher-priority transactions are evicted in its place. The corruption persists indefinitely — there is no self-healing path unless A itself is later removed or a new descendant is added that triggers `update_ancestors_index_key` with `Add`.

Secondary effect: `descendants_fee` inflation also distorts `AncestorsScoreSortKey` indirectly through the `EvictKey.fee_rate` field, misrepresenting A's effective fee rate to the eviction subsystem.

---

### Likelihood Explanation

The bug is triggered by any call to `remove_entry_and_descendants` where the removed root has at least one ancestor still in the pool. This occurs in multiple reachable code paths:

1. **RBF replacement** (`process_rbf`): an unprivileged tx-pool submitter sends a replacement transaction that conflicts with B (spending a confirmed input also spent by B, but not A's output). B is removed; A remains with a stale `evict_key`. [12](#0-11) 

2. **Committed-tx conflict resolution** (`resolve_conflict` inside `remove_committed_tx`): a miner commits a transaction that double-spends B's input. B and its descendants are removed; A is left with stale statistics. [13](#0-12) 

3. **Detached-proposal reorg** (`remove_by_detached_proposal`): B is removed and re-added as Pending. During the window between removal and re-insertion, `limit_size` (called at the end of `_update_tx_pool_for_reorg`) may evict wrong entries. [14](#0-13) 

Path 1 requires no mining power and is fully controllable by an unprivileged tx-pool submitter.

---

### Recommendation

Move the ancestor-score update **before** the link teardown, or collect the ancestor set before removing links and pass it explicitly to `remove_entry`. A minimal fix:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Collect ancestor sets BEFORE removing any links
    let ancestor_sets: Vec<_> = removed_ids
        .iter()
        .map(|id| (id.clone(), self.links.calc_ancestors(id)))
        .collect();

    for id in &removed_ids {
        self.remove_entry_links(id);
    }

    for (id, ancestors) in &ancestor_sets {
        if let Some(entry) = self.entries.get_by_id(id) {
            let inner = entry.inner.clone();
            for anc_id in ancestors {
                if !removed_ids.contains(anc_id) {
                    self.entries.modify_by_id(anc_id, |e| {
                        e.inner.sub_descendant_weight(&inner);
                        e.evict_key = e.inner.as_evict_key();
                    });
                }
            }
        }
    }

    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Alternatively, add a dedicated `remove_entry_batch` path that skips the ancestor update inside `remove_entry` (via a flag) and performs a single correct ancestor update pass before link teardown.

---

### Proof of Concept

**Setup**: Pool contains A → B → C (A is parent of B, B is parent of C). All three are in `Proposed` status.

**Step 1**: Submit a new transaction D via RBF that spends a confirmed input X also spent by B (but D does not spend A's output). D passes RBF checks; `process_rbf` calls `remove_entry_and_descendants(B)`.

**Step 2**: Inside `remove_entry_and_descendants`:
- `removed_ids = [B, C]`
- `remove_entry_links(B)`: removes B from A's children list; removes B's own link entry.
- `remove_entry_links(C)`: removes C's own link entry.
- `remove_entry(B)`: calls `update_ancestors_index_key(B, Remove)` → `calc_ancestors(B)` → B's link entry is gone → returns `{}` → **A's `descendants_count` is NOT decremented**.
- `remove_entry(C)`: same — A's statistics remain inflated.

**Step 3**: Inspect A's state:
- `A.inner.descendants_count` = 3 (should be 1 — only itself)
- `A.inner.descendants_fee` = fee(A) + fee(B) + fee(C) (should be fee(A) only)
- `A.evict_key.fee_rate` = inflated (includes B's and C's fees)
- `A.evict_key.descendants_count` = 3 (should be 1)

**Step 4**: Fill the pool to trigger `limit_size`. A is skipped for eviction because its `evict_key` is artificially high. A legitimate transaction with a genuinely higher fee rate but lower (correct) `evict_key` is evicted instead.

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

**File:** tx-pool/src/component/pool_map.rs (L252-259)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L261-265)
```rust
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

**File:** tx-pool/src/component/links.rs (L94-96)
```rust
    pub fn remove(&mut self, short_id: &ProposalShortId) -> Option<TxLinks> {
        self.inner.remove(short_id)
    }
```

**File:** tx-pool/src/component/entry.rs (L35-42)
```rust
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
    /// The unix timestamp when entering the Txpool, unit: Millisecond
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

**File:** tx-pool/src/process.rs (L190-234)
```rust
    fn process_rbf(
        &self,
        tx_pool: &mut TxPool,
        entry: &TxEntry,
        conflicts: &HashSet<ProposalShortId>,
    ) -> Vec<TransactionView> {
        let mut may_recovered_txs = vec![];
        let mut available_inputs = HashSet::new();

        if conflicts.is_empty() {
            return may_recovered_txs;
        }

        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();

        available_inputs.extend(
            all_removed
                .iter()
                .flat_map(|removed| removed.transaction().input_pts_iter()),
        );

        for input in entry.transaction().input_pts_iter() {
            available_inputs.remove(&input);
        }

        may_recovered_txs = tx_pool.get_conflicted_txs_from_inputs(available_inputs.into_iter());
        for old in all_removed {
            debug!(
                "remove conflict tx {} for RBF by new tx {}",
                old.transaction().hash(),
                entry.transaction().hash()
            );
            let reject =
                Reject::RBFRejected(format!("replaced by tx {}", entry.transaction().hash()));

            // RBF replace successfully, put old transactions into conflicts pool
            tx_pool.record_conflict(old.transaction().clone());
            // after removing old tx from tx_pool, we call reject callbacks manually
            self.callbacks.call_reject(tx_pool, &old, reject);
        }
        assert!(!may_recovered_txs.contains(entry.transaction()));
        may_recovered_txs
```

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
```
