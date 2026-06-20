### Title
Stale Descendant-Weight Accounting in `remove_entry_and_descendants` Allows Attacker to Inflate Ancestor Evict-Key, Causing Legitimate Transactions to Be Rejected — (`File: tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` pre-removes all parent/child links for every entry in the subtree **before** calling `remove_entry` on each one. Because `update_ancestors_index_key` resolves ancestors through those same links, it finds an empty ancestor set and silently skips updating the surviving ancestors' `descendants_*` fields. The result is that any ancestor of the removed subtree retains permanently inflated `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count`. This corrupts the `EvictKey` of those ancestors, making them appear more valuable than they are and preventing them from being evicted when the pool is full. An unprivileged attacker can exploit this to keep a low-fee transaction in the pool indefinitely and force legitimate higher-fee transactions to receive `Reject::Full`.

---

### Finding Description

**Two removal paths exist with asymmetric behavior:**

**Path 1 — `remove_entry` (single entry):** [1](#0-0) 

`update_ancestors_index_key` is called **while links are still intact**, so `calc_ancestors` returns the correct ancestor set and each ancestor's `sub_descendant_weight` is called.

**Path 2 — `remove_entry_and_descendants` (subtree):** [2](#0-1) 

All links for every entry in the subtree are removed **first** (lines 257–259), then `remove_entry` is called for each. When `remove_entry` reaches `update_ancestors_index_key`, `calc_ancestors` traverses the now-empty link map and returns an empty set: [3](#0-2) 

No ancestor's `sub_descendant_weight` is ever called. The comment on line 256 acknowledges the intent to skip `update_descendants_index_key` (updating children's ancestor weights, which is harmless since children are being removed), but the same pre-removal of links also silently suppresses `update_ancestors_index_key` (updating **parents'** descendant weights, which is harmful since parents remain in the pool).

**The stale fields feed directly into `EvictKey`:** [4](#0-3) 

`fee_rate` is `descendants_feerate.max(feerate)`. If the removed child had a high fee rate, the parent's `EvictKey.fee_rate` remains inflated after the child is gone.

**`next_evict_entry` picks the entry with the lowest `EvictKey`:** [5](#0-4) 

An ancestor with an inflated evict key is never selected for eviction, even when its actual fee rate is the lowest in the pool.

**`limit_size` uses `next_evict_entry` and issues `Reject::Full` for the evicted entry:** [6](#0-5) 

---

### Impact Explanation

An ancestor transaction with stale `descendants_*` fields will never be chosen by `next_evict_entry` when the pool is full. Instead, `limit_size` evicts other transactions — potentially the legitimate transaction just submitted — and returns `Reject::Full` to the submitter. The attacker's low-fee transaction occupies pool space indefinitely, blocking legitimate users.

---

### Likelihood Explanation

`remove_entry_and_descendants` is called in every conflict-resolution and RBF path, as well as during size-limit enforcement and detached-proposal handling: [7](#0-6) [8](#0-7) 

Any tx-pool submitter (RPC caller via `send_transaction`) can trigger this without any privileged access. The attacker only needs to submit a parent transaction and a high-fee child, then replace the child via RBF or a conflicting input, leaving the parent with a permanently inflated evict key.

---

### Recommendation

Before pre-removing links in `remove_entry_and_descendants`, collect the surviving ancestors of the root entry and call `sub_descendant_weight` on each of them for every entry in the removed subtree. Alternatively, restructure the function so that `update_ancestors_index_key` is called for each removed entry **before** its links are removed, mirroring the behavior of the single-entry `remove_entry` path.

---

### Proof of Concept

1. Submit `tx_A` (low fee, e.g. 1 shannon/byte) — it enters the pool.
2. Submit `tx_B` spending an output of `tx_A` with a very high fee (e.g. 1000 shannons/byte). `tx_A`'s `descendants_fee` is now `fee_A + fee_B`; its `EvictKey.fee_rate` is inflated.
3. Submit `tx_C` that spends the same input as `tx_B` with a slightly higher fee (RBF). `tx_B` is removed via `remove_entry_and_descendants`. Because links are pre-removed, `tx_A`'s `descendants_fee` is **not** decremented; it still reflects `fee_A + fee_B`.
4. Fill the pool to capacity with other transactions whose actual fee rates are between `tx_A`'s actual rate and its inflated rate.
5. Submit a legitimate `tx_D` with a moderate fee rate. `limit_size` is triggered. `next_evict_entry` skips `tx_A` (inflated evict key) and evicts `tx_D` or another legitimate transaction instead. `tx_D` receives `Reject::Full`.

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

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```
