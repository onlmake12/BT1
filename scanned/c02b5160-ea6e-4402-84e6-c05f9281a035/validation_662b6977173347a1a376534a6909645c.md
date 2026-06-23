### Title
Stale `descendants_fee` / `descendants_size` After `remove_entry_and_descendants` Allows Eviction-Priority Manipulation — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all parent/child links are severed **before** `remove_entry` is called for each removed transaction. Because `update_ancestors_index_key` relies on `links.calc_ancestors` to find which remaining pool entries need their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` decremented, and because those links are already gone at call time, the ancestors of the removed subtree are **never updated**. Their descendant-weight fields remain permanently inflated, corrupting the `EvictKey` used to decide which transactions to drop when the pool is full.

---

### Finding Description

`remove_entry_and_descendants` first calls `remove_entry_links` for every ID in the batch, then calls `remove_entry` for each:

```rust
// tx-pool/src/component/pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);          // ← severs ALL links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
``` [1](#0-0) 

Inside `remove_entry`, `update_ancestors_index_key` is called to decrement the `descendants_*` fields of every ancestor that **remains** in the pool:

```rust
// pool_map.rs  lines 432-445
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
``` [2](#0-1) 

Because `remove_entry_links` already removed the entry from `self.links`, `calc_ancestors` returns an empty set. The `sub_descendant_weight` call that should decrement `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` on surviving ancestors is **never executed**. [3](#0-2) 

The `EvictKey` for each surviving ancestor is computed from these stale fields:

```rust
// entry.rs  lines 234-247
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
``` [4](#0-3) 

The single-entry path (`remove_entry` called directly, not via `remove_entry_and_descendants`) does **not** have this bug because `remove_entry_links` is called **after** `update_ancestors_index_key`, so `calc_ancestors` still finds the correct ancestors. [5](#0-4) 

---

### Impact Explanation

`EvictKey` drives `next_evict_entry`, which is called by `limit_size` to decide which transaction to drop when the pool exceeds `max_tx_pool_size`:

```rust
// pool.rs  lines 290-328
pub(crate) fn limit_size(...) {
    while self.pool_map.total_tx_size > self.config.max_tx_pool_size {
        if let Some(id) = next_evict_entry() {
            let removed = self.pool_map.remove_entry_and_descendants(&id);
            ...
        }
    }
}
``` [6](#0-5) 

A surviving ancestor with a stale (inflated) `descendants_fee` appears to have a higher fee rate than it actually does, so it is ranked **below** legitimate high-fee transactions in the eviction order and is not dropped. Legitimate high-fee transactions are evicted in its place.

---

### Likelihood Explanation

`remove_entry_and_descendants` is called from four reachable code paths, all triggerable by an unprivileged peer or RPC caller:

1. **`resolve_conflict`** — triggered whenever a submitted transaction conflicts with a pool entry (standard double-spend or RBF replacement via `send_transaction` RPC).
2. **`resolve_conflict_header_dep`** — triggered on any block reorg that invalidates a header dep.
3. **`limit_size`** — triggered automatically when the pool exceeds `max_tx_pool_size`.
4. **`check_and_record_ancestors`** — triggered when ancestor count exceeds `max_ancestors_count`. [7](#0-6) 

The simplest exploit requires only two `send_transaction` calls (tx0 low-fee parent, tx1 high-fee child) followed by a conflicting replacement for tx1. After the replacement, tx0's `descendants_fee` is permanently inflated, making it eviction-resistant even when the pool is full.

---

### Recommendation

Before severing links in `remove_entry_and_descendants`, collect the set of **external ancestors** (ancestors not in `removed_ids`) and update their `descendants_*` fields first:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();

    // Update surviving ancestors BEFORE severing links
    for removed_id in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(removed_id) {
            let entry_inner = entry.inner.clone();
            let ancestors = self.links.calc_ancestors(removed_id);
            for anc_id in ancestors.iter().filter(|a| !removed_set.contains(*a)) {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&entry_inner);
                    e.evict_key = e.inner.as_evict_key();
                });
            }
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

Alternatively, restructure `remove_entry` so that `update_ancestors_index_key` is called before `remove_entry_links`, matching the invariant already upheld by the single-entry removal path.

---

### Proof of Concept

**Setup:** Pool with tx0 (low fee, e.g. 100 shannons) and tx1 (high fee, e.g. 10 000 shannons, spending tx0's output). After insertion, tx0's `descendants_fee = 10 100`, `descendants_count = 2`.

**Trigger:** Submit tx1' (conflicting with tx1, fee > tx1 + rbf_extra). `resolve_conflict` calls `remove_entry_and_descendants(&tx1_id)`.

**Result:** tx1 is removed. tx0's `descendants_fee` remains `10 100` (should be `100`). tx0's `EvictKey.fee_rate` is computed from the inflated `descendants_fee`, making it appear as a high-fee transaction.

**Effect:** Fill the pool to capacity with medium-fee transactions. `limit_size` evicts medium-fee transactions before tx0, even though tx0's real fee rate is the lowest in the pool. The attacker keeps a near-zero-fee transaction in the pool indefinitely at the cost of one RBF replacement fee. [1](#0-0) [2](#0-1) [4](#0-3)

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

**File:** tx-pool/src/pool.rs (L290-328)
```rust
    // Remove transactions from the pool until total size <= size_limit.
    // Return a `Reject` for current inserting entry if it's removed
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
```
