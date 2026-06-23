### Title
`remove_entry_and_descendants` Skips Ancestor `descendants_*` Decrement, Inflating Surviving Entries' Descendant Accounting — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all link records for the removed subtree are torn down **before** `remove_entry` is called on each entry. Because `update_ancestors_index_key` resolves ancestors through the live link map, it finds nothing and silently skips decrementing the `descendants_count / descendants_size / descendants_cycles / descendants_fee` fields of any ancestor that survives outside the removed subtree. Those fields are permanently inflated for the lifetime of the surviving ancestor in the pool.

---

### Finding Description

`remove_entry_and_descendants` is the function used to atomically remove a transaction and its entire descendant subtree from the pool (called during RBF conflict resolution, size-limit eviction, and block-commit cleanup). [1](#0-0) 

The function first collects all descendant IDs, then calls `remove_entry_links` for **every** entry in the set — including the root — before any `remove_entry` call:

```
for id in &removed_ids {
    self.remove_entry_links(id);   // ← strips id from links map
}
removed_ids.iter().filter_map(|id| self.remove_entry(id)).collect()
```

`remove_entry_links` removes the entry from the `TxLinksMap` entirely: [2](#0-1) 

When `remove_entry` is subsequently called, it invokes `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`: [3](#0-2) 

`update_ancestors_index_key` resolves ancestors by calling `self.links.calc_ancestors(&child.proposal_short_id())`: [4](#0-3) 

Because the root entry's link record was already deleted by `remove_entry_links`, `calc_ancestors` returns an empty set. The loop body never executes, and `sub_descendant_weight` is never called on any surviving ancestor: [5](#0-4) 

The developer comment acknowledges the link-removal side-effect only for `update_descendants_index_key` (intentionally skipped because those entries are being removed anyway), but the same mechanism also silently suppresses the necessary ancestor update.

---

### Impact Explanation

Every surviving ancestor of the removed subtree retains stale, inflated values for `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee`: [6](#0-5) 

These fields feed directly into `EvictKey`, which governs which transactions are selected for eviction when the pool exceeds `max_tx_pool_size`: [7](#0-6) 

An ancestor with inflated `descendants_fee` and `descendants_count` appears more valuable than it actually is, making it resistant to eviction. The pool's `total_tx_size` is correctly decremented (via `update_stat_for_remove_tx`), but the per-entry descendant metadata diverges from reality permanently until the ancestor itself is removed. [8](#0-7) 

Consequences:
- **Incorrect eviction ordering**: Low-fee ancestors survive eviction rounds they should lose, displacing legitimate higher-fee transactions.
- **Pool bloat / transaction censorship**: An attacker can keep a low-fee parent transaction alive indefinitely by repeatedly submitting and replacing child transactions, each removal inflating the parent's apparent descendant value further.
- **Block template degradation**: `sorted_proposed_iter` and score-sorted iterators used during block assembly rely on accurate per-entry metadata; stale values cause suboptimal transaction selection.

---

### Likelihood Explanation

The trigger path is fully reachable by any unprivileged RPC caller via `send_transaction`. The attacker submits a parent transaction (tx_A, low fee) and a child transaction (tx_B, high fee). When tx_B is replaced via RBF or evicted by the size limiter, `remove_entry_and_descendants(tx_B)` is called, leaving tx_A's `descendants_*` permanently inflated. No special privileges, keys, or majority hash power are required. The scenario is reproducible with two ordinary transactions. [9](#0-8) 

---

### Recommendation

Move the ancestor index update for the **root** entry to occur **before** its links are removed. One approach:

1. Before the `remove_entry_links` loop, call `update_ancestors_index_key(root_entry, EntryOp::Remove)` while the root's link record is still intact.
2. Then proceed with the existing link-removal loop and `remove_entry` calls (which will correctly skip ancestor updates for the already-handled root, and skip descendant updates for entries being removed).

Alternatively, collect the set of surviving ancestors of the root before any link removal, and explicitly call `sub_descendant_weight` on each of them after the subtree is removed.

---

### Proof of Concept

```
Chain: tx_A (parent, fee=1 shannon) → tx_B (child, fee=10000 shannons)

1. submit tx_A  → pool accepts; tx_A.descendants_fee = 1 (self only)
2. submit tx_B  → pool accepts; tx_A.descendants_fee = 10001 (self + tx_B)
3. submit tx_B' replacing tx_B via RBF
   → remove_entry_and_descendants(tx_B) is called
   → remove_entry_links(tx_B) strips tx_B from links map
   → remove_entry(tx_B): calc_ancestors(tx_B) == {} (links gone)
   → tx_A.descendants_fee remains 10001  ← BUG: should be 1
4. Pool is now near max_tx_pool_size; eviction runs
   → tx_A.EvictKey.fee_rate is computed from inflated descendants_fee=10001
   → tx_A survives eviction despite having no real high-fee descendants
   → a legitimate high-fee transaction is rejected instead
``` [1](#0-0) [4](#0-3) [5](#0-4)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L235-249)
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

**File:** tx-pool/src/component/pool_map.rs (L731-758)
```rust
    /// Update size and cycles statistics for remove tx
    /// cycles overflow is possible, currently obtaining cycles is not accurate
    fn update_stat_for_remove_tx(&mut self, tx_size: usize, cycles: Cycle) {
        match (
            self.total_tx_size.checked_sub(tx_size),
            self.total_tx_cycles.checked_sub(cycles),
        ) {
            (Some(total_tx_size), Some(total_tx_cycles)) => {
                self.total_tx_size = total_tx_size;
                self.total_tx_cycles = total_tx_cycles;
            }
            _ => {
                if let Some((total_tx_size, total_tx_cycles)) = self.recompute_total_stat() {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, recomputed size {} cycles {}",
                        tx_size, cycles, total_tx_size, total_tx_cycles
                    );
                    self.total_tx_size = total_tx_size;
                    self.total_tx_cycles = total_tx_cycles;
                } else {
                    error!(
                        "tx-pool total stats underflowed when removing size {} cycles {}, and recomputing overflowed",
                        tx_size, cycles
                    );
                }
            }
        }
    }
```

**File:** tx-pool/src/component/entry.rs (L15-44)
```rust
/// An entry in the transaction pool.
#[derive(Debug, Clone, Eq)]
pub struct TxEntry {
    /// Transaction
    pub rtx: Arc<ResolvedTransaction>,
    /// Cycles
    pub cycles: Cycle,
    /// tx size
    pub size: usize,
    /// fee
    pub fee: Capacity,
    /// ancestors txs size
    pub ancestors_size: usize,
    /// ancestors txs fee
    pub ancestors_fee: Capacity,
    /// ancestors txs cycles
    pub ancestors_cycles: Cycle,
    /// ancestors txs count
    pub ancestors_count: usize,
    /// descendants txs fee
    pub descendants_fee: Capacity,
    /// descendants txs size
    pub descendants_size: usize,
    /// descendants txs cycles
    pub descendants_cycles: Cycle,
    /// descendants txs count
    pub descendants_count: usize,
    /// The unix timestamp when entering the Txpool, unit: Millisecond
    pub timestamp: u64,
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
