### Title
Stale `descendants_*` Accounting in `remove_entry_and_descendants` Leaves Ancestor `EvictKey` Inflated — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`remove_entry_and_descendants` pre-removes all link records before calling `remove_entry` on each evicted entry. Because `update_ancestors_index_key` relies on `calc_ancestors` (which reads those same link records), it finds no ancestors and never decrements the surviving parent's `descendants_fee / descendants_size / descendants_cycles / descendants_count`. The parent's `EvictKey` therefore remains permanently inflated, making it appear more valuable than it is and suppressing its eviction priority.

---

### Finding Description

`remove_entry_and_descendants` operates in two phases:

**Phase 1 — strip all links first:**
```rust
for id in &removed_ids {
    self.remove_entry_links(id);   // erases id from self.links.inner
}
```

**Phase 2 — remove each entry:**
```rust
removed_ids.iter()
    .filter_map(|id| self.remove_entry(id))
    .collect()
```

Inside `remove_entry`, the first thing called is:

```rust
self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
```

which does:

```rust
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors = self.links.calc_ancestors(&child.proposal_short_id());
    // ^^^ returns {} because remove_entry_links already erased child's record
    for anc_id in &ancestors {          // loop body never executes
        self.entries.modify_by_id(anc_id, |e| {
            e.inner.sub_descendant_weight(child);
            e.evict_key = e.inner.as_evict_key();
        });
    }
}
```

Because `child`'s link record was deleted in Phase 1, `calc_ancestors` returns an empty set. The surviving parent `tx_A` (which is **not** in `removed_ids`) never has `sub_descendant_weight` called on it, so its `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain at their pre-removal values. The stored `evict_key` in the `PoolEntry` is therefore stale and inflated.

The comment in the source even acknowledges the intent to skip `update_descendants_index_key` for entries being removed, but the side-effect of also skipping `update_ancestors_index_key` for **surviving** ancestors is unintentional: [1](#0-0) 

The correct single-entry removal path (`remove_entry` called directly) does **not** have this problem because it calls `update_ancestors_index_key` **before** `remove_entry_links`: [2](#0-1) 

The `EvictKey` that is left stale is computed from the inflated `descendants_fee` / `descendants_size` / `descendants_cycles`: [3](#0-2) 

---

### Impact Explanation

`EvictKey` drives which transaction is chosen for eviction when the pool exceeds `max_tx_pool_size`: [4](#0-3) 

A surviving ancestor whose `descendants_feerate` is inflated will be ranked as more valuable than it actually is, so it will be skipped during eviction. Legitimate high-fee-rate transactions submitted by other users may be evicted in its place. The attacker can keep a low-fee transaction alive in the pool indefinitely at the cost of a single RBF cycle, degrading pool fairness and potentially blocking honest transactions from entering.

---

### Likelihood Explanation

The trigger path is `resolve_conflict → remove_entry_and_descendants`, which is reached whenever a submitted transaction conflicts with an existing pool entry (standard double-spend or RBF): [5](#0-4) 

An unprivileged RPC caller or P2P relay peer can reach this path by:
1. Submitting `tx_A` (low fee-rate).
2. Submitting `tx_B` (child of `tx_A`, high fee-rate) — `tx_A`'s `descendants_*` stats are correctly inflated.
3. Submitting `tx_B'` (conflicting input with `tx_B`, RBF-valid) — `remove_entry_and_descendants(tx_B)` is called, but `tx_A`'s stats are **not** decremented.
4. `tx_A` now permanently carries inflated `descendants_feerate` in its `EvictKey`.

RBF must be enabled (`min_rbf_rate > min_fee_rate`), which is a configurable but common deployment choice. No privileged access is required.

---

### Recommendation

Before stripping links in `remove_entry_and_descendants`, collect the set of **surviving** ancestors (those not in `removed_ids`) and update their `descendants_*` stats and `evict_key` explicitly. One approach:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    let removed_set: HashSet<_> = removed_ids.iter().cloned().collect();

    // For each entry being removed, update surviving ancestors BEFORE links are erased
    for rid in &removed_ids {
        if let Some(entry) = self.entries.get_by_id(rid) {
            let entry_inner = entry.inner.clone();
            let ancestors = self.links.calc_ancestors(rid);
            for anc_id in ancestors {
                if !removed_set.contains(&anc_id) {
                    self.entries.modify_by_id(&anc_id, |e| {
                        e.inner.sub_descendant_weight(&entry_inner);
                        e.evict_key = e.inner.as_evict_key();
                    });
                }
            }
        }
    }

    for id in &removed_ids {
        self.remove_entry_links(id);
    }
    removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
}
```

---

### Proof of Concept

```
Pool state:
  tx_A  (fee=1 shannon/byte, size=200)  → descendants_fee = 1000 (from tx_B)
  tx_B  (fee=1000 shannons/byte, size=200, spends tx_A output 0)

Step 1: Attacker submits tx_B' (spends same output as tx_B, fee > tx_B → valid RBF).
Step 2: resolve_conflict() calls remove_entry_and_descendants(tx_B).
Step 3: remove_entry_links(tx_B) erases tx_B's link record.
Step 4: remove_entry(tx_B) → update_ancestors_index_key finds no ancestors → tx_A untouched.

Pool state after:
  tx_A  (fee=1 shannon/byte)  → descendants_fee still = 1000  ← STALE
  tx_B' (fee=1001 shannons/byte)

When pool is full and eviction runs, tx_A's EvictKey shows
descendants_feerate ≈ 1000/200 = 5 shannons/byte instead of 0,
so tx_A is ranked above legitimate 3-shannon/byte transactions
and survives eviction it should not survive.
```

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

**File:** tx-pool/src/pool.rs (L298-326)
```rust
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
```
