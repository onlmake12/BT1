### Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor Entries with Inflated Eviction Keys — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry_and_descendants` pre-removes all link relationships for every entry in the subtree before calling `remove_entry` on each. This prevents `update_ancestors_index_key` from locating and updating the `descendants_*` fields and `evict_key` of ancestor entries that **remain** in the pool. Those ancestors permanently carry inflated descendant-fee/size/cycle accounting, causing incorrect eviction ordering in `limit_size` and enabling a tx-pool resource-accounting DoS.

---

### Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1 — pre-remove all links:** [1](#0-0) 

```rust
// update links state for remove, so that we won't update_descendants_index_key in remove_entry
for id in &removed_ids {
    self.remove_entry_links(id);   // ← removes id from self.links entirely
}
```

**Phase 2 — remove each entry:**

```rust
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

Inside `remove_entry`, the first thing called is `update_ancestors_index_key`: [2](#0-1) 

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

`update_ancestors_index_key` resolves ancestors by calling `self.links.calc_ancestors(&child.proposal_short_id())`: [3](#0-2) 

Because Phase 1 already called `self.links.remove(id)` for every entry in the subtree, `calc_ancestors` returns an **empty set** for every removed entry. The loop body that calls `e.inner.sub_descendant_weight(child)` and updates `e.evict_key` is never reached for any ancestor that **remains** in the pool.

The `EvictKey` stored in `PoolEntry` for those ancestors is therefore never corrected: [4](#0-3) 

```rust
EvictKey {
    fee_rate: descendants_feerate.max(feerate),   // ← uses stale descendants_fee/size/cycles
    descendants_count: entry.descendants_count,   // ← stale
    ...
}
```

The `descendants_*` fields on `TxEntry` are also never decremented: [5](#0-4) 

---

### Impact Explanation

`limit_size` evicts entries by calling `next_evict_entry`, which iterates `entries` ordered by `evict_key`: [6](#0-5) 

An ancestor whose high-fee descendants were removed still carries their fee contribution in `descendants_fee`. Its `evict_key.fee_rate = descendants_feerate.max(feerate)` is inflated, so it sorts as **more valuable** than it actually is and is skipped during eviction. The pool fills with these "zombie" low-fee ancestors that appear high-value, causing legitimate high-fee transactions to be rejected with `Reject::Full`.

---

### Likelihood Explanation

`remove_entry_and_descendants` is called from multiple reachable paths:

1. **`resolve_conflict`** — triggered by any committed block that spends an input already in the pool. A block relayer submitting a valid block causes this.
2. **`check_and_record_ancestors`** — triggered by an unprivileged `send_transaction` RPC caller who submits a transaction that forces eviction of cell-ref parents.
3. **`limit_size`** — triggered by pool-fill pressure from any tx submitter.

Path 2 is directly reachable by an unprivileged tx-pool submitter with no mining power required.

---

### Recommendation

Before pre-removing links in `remove_entry_and_descendants`, collect the set of **external ancestors** (ancestors of the root that are not themselves in `removed_ids`) and call `sub_descendant_weight` + update `evict_key` on each of them for every entry being removed. Alternatively, restructure the function to call `update_ancestors_index_key` before `remove_entry_links` for the root entry only (since descendants' ancestor fields need not be updated — they are being removed).

---

### Proof of Concept

```
Pool state:
  tx_A (fee=1 shannon, size=100) — pending
    └─ tx_B (fee=1000 shannons, size=100) — pending (child of tx_A)

After add_entry(tx_A) then add_entry(tx_B):
  tx_A.descendants_fee   = 1001 shannons
  tx_A.descendants_count = 2
  tx_A.evict_key.fee_rate ≈ 1001/200 (high)

Trigger: block commits tx_C which spends the same input as tx_B.
  → remove_committed_tx(tx_C) → resolve_conflict → remove_entry_and_descendants(tx_B)

After removal:
  tx_B is gone.
  tx_A.descendants_fee   = 1001 shannons  ← STALE (should be 1)
  tx_A.descendants_count = 2              ← STALE (should be 1)
  tx_A.evict_key.fee_rate ≈ 1001/200      ← STALE (should be 1/100)

Pool fills up. limit_size() calls next_evict_entry().
tx_A is skipped because its evict_key shows high fee rate.
A legitimate tx with fee=500 shannons is rejected with Reject::Full.
``` [1](#0-0) [7](#0-6) [2](#0-1) [8](#0-7)

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

**File:** tx-pool/src/pool.rs (L292-328)
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
```
