### Title
Stale Ancestor `descendants_fee` Accounting After Subtree Removal Enables Pool Eviction Manipulation - (File: `tx-pool/src/component/pool_map.rs`)

### Summary
`remove_entry_and_descendants` pre-removes all link entries before calling `remove_entry`, which causes `update_ancestors_index_key` to silently skip updating the `descendants_fee` of surviving ancestor transactions. An unprivileged tx-pool submitter can exploit this via repeated RBF replacements to inflate a low-fee transaction's apparent `descendants_fee`, making it immune to eviction and enabling pool-filling attacks.

### Finding Description

`remove_entry_and_descendants` first strips all link entries for the entire removed subtree, then calls `remove_entry` for each: [1](#0-0) 

Inside `remove_entry`, the ancestor update path is: [2](#0-1) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())` to find which surviving entries need their `descendants_fee` decremented: [3](#0-2) 

Because `remove_entry_links` was already called for the subtree root before `remove_entry` runs, the root's own link record is gone. `calc_ancestors` traverses from the root's parents, but the root has no link entry, so it returns an empty set. The surviving ancestors of the root — which are **not** in the removed set — never receive the `sub_descendant_weight` call. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain permanently inflated.

The `sub_descendant_weight` function that should have been called uses `saturating_sub`, meaning any future underflow would silently zero the field rather than panic: [4](#0-3) 

The stale `descendants_fee` directly feeds the `EvictKey` used for pool eviction ordering: [5](#0-4) 

`resolve_conflict` — the RBF path — calls `remove_entry_and_descendants`, making this reachable by any tx-pool submitter: [6](#0-5) 

The pool's size-limit eviction loop also calls `remove_entry_and_descendants`, so the same stale accounting affects eviction under memory pressure: [7](#0-6) 

### Impact Explanation

A surviving ancestor transaction retains an inflated `descendants_fee`. Its `EvictKey` is computed as `descendants_feerate.max(feerate)`. With an artificially high `descendants_feerate`, the entry ranks as highly valuable and is never selected for eviction. An attacker can use this to:

1. Keep a near-zero-fee parent transaction permanently in the pool.
2. Repeatedly inflate its apparent value by submitting and RBF-replacing high-fee children, each replacement leaving a stale fee increment that is never subtracted.
3. Fill the pool with such entries, causing legitimate transactions to be rejected with `Reject::Full`.

The `total_tx_size` aggregate is separately corrected via `recompute_total_stat` on underflow, but `descendants_fee` has no such correction path. [8](#0-7) 

### Likelihood Explanation

Any node's RPC endpoint accepts `send_transaction` from unprivileged callers. RBF is enabled whenever `min_rbf_rate > min_fee_rate`. The attacker pays incrementally higher fees per replacement (RBF requirement), but each replacement permanently inflates the parent's `descendants_fee` by the replaced child's fee. After N replacements the inflation is `sum(child_fees[0..N-1])`, which grows without bound. No privileged access, key material, or majority hashpower is required.

### Recommendation

Before calling `remove_entry_links` for the subtree root, collect the root's surviving ancestors and apply `sub_descendant_weight` to each of them. Concretely, in `remove_entry_and_descendants`, compute `calc_ancestors(id)` for the root **before** any link removal, then iterate over those ancestor IDs and call `sub_descendant_weight` with the root entry's weight. This mirrors the logic already present in `update_ancestors_index_key` but executed before links are torn down.

### Proof of Concept

```
// Setup: P is a low-fee parent, C1 is a high-fee child spending P's output.
// After adding both, P.descendants_fee = P.fee + C1.fee.

// Attacker submits C1' (higher fee, same input as C1) → RBF triggers resolve_conflict:
//   remove_entry_and_descendants(C1_id)
//     → remove_entry_links(C1_id)   // severs P→C1 link
//     → remove_entry(C1_id)
//         → update_ancestors_index_key(C1, Remove)
//             → calc_ancestors(C1_id) == {} (link already gone)
//             → P.descendants_fee NOT decremented  ← BUG
//   C1' added as new child of P → P.descendants_fee += C1'.fee

// After one replacement:
//   P.descendants_fee = P.fee + C1.fee (stale) + C1'.fee
//                                ^^^^^^^^ never removed

// After N replacements:
//   P.descendants_fee = P.fee + sum(C1..CN fees) + CN'.fee
// P's EvictKey reflects this inflated value → P is never evicted.
// Attacker fills pool with such entries; legitimate txs receive Reject::Full.
``` [1](#0-0) [3](#0-2) [4](#0-3)

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

**File:** tx-pool/src/component/pool_map.rs (L305-331)
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

**File:** tx-pool/src/pool.rs (L298-328)
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
        self.pool_map.entries.shrink_to_fit();
        ret
```
