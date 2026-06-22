### Title
Stale Descendant-Weight Bookkeeping After `remove_entry_and_descendants` Corrupts Eviction Order — (`File: tx-pool/src/component/pool_map.rs`)

### Summary
`remove_entry_and_descendants` strips all link records before invoking `remove_entry` on each member of the removed subtree. Because `remove_entry` relies on the live link graph to locate ancestors and update their `descendants_*` fields and `evict_key`, those fields are never corrected for the surviving parent of the removed subtree. The result is a permanently inflated `EvictKey` on the surviving parent, which causes the pool's size-limit eviction loop to skip over it and instead evict a higher-fee-rate transaction — the exact analogue of FraxSwap's stale `twammReserve` causing over-transfer.

### Finding Description

`remove_entry_and_descendants` is the function called during RBF replacement (`process_rbf`), conflict resolution (`resolve_conflict`), and size-limit enforcement (`limit_size`):

```rust
// pool_map.rs  lines 252-265
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);   // ← strips every entry from self.links.inner
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))
        .collect()
}
```

`remove_entry_links` calls `self.links.remove(id)`, which deletes the entry from `self.links.inner`. [1](#0-0) 

`remove_entry` then calls `update_ancestors_index_key(&entry.inner, EntryOp::Remove)`:

```rust
// pool_map.rs  lines 432-445
fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
    let ancestors: HashSet<ProposalShortId> =
        self.links.calc_ancestors(&child.proposal_short_id());
    for anc_id in &ancestors {
        self.entries.modify_by_id(anc_id, |e| {
            match op {
                EntryOp::Remove => e.inner.sub_descendant_weight(child),
                ...
            };
            e.evict_key = e.inner.as_evict_key();   // ← never reached for the surviving parent
        });
    }
}
``` [2](#0-1) 

Because `self.links.inner` no longer contains the removed entry, `calc_ancestors` returns an empty set. The surviving parent of the removed subtree — which is **not** in `removed_ids` — never has `sub_descendant_weight` called on it and never has its `evict_key` recomputed. Its `descendants_count`, `descendants_size`, `descendants_cycles`, and `descendants_fee` remain at their pre-removal values indefinitely.

The `EvictKey` ordering evicts the entry with the **smallest** key first, where a higher `descendants_count` produces a larger key: [3](#0-2) 

The size-limit eviction loop in `limit_size` therefore skips the surviving parent (whose `descendants_count` is artificially high) and evicts a different, potentially higher-fee-rate transaction instead: [4](#0-3) 

The same stale `evict_key` persists until the parent is itself removed or the node restarts.

### Impact Explanation

An unprivileged tx-pool submitter can:

1. Submit `tx_A` (low fee-rate, pending).
2. Submit `tx_B` (child of `tx_A`, high fee-rate).
3. Submit `tx_C` that RBF-replaces `tx_B` (or wait for `tx_B` to be evicted by size limit).
4. `remove_entry_and_descendants` is called for `tx_B`; `tx_A`'s `descendants_count` stays at 2 instead of dropping to 1.
5. When the pool is full and `limit_size` runs, `tx_A`'s inflated `EvictKey` causes it to be skipped; a legitimate higher-fee-rate transaction is evicted instead and receives `Reject::Full`.

The attacker keeps a below-minimum-fee-rate transaction alive in the pool beyond its intended lifetime, and can cause legitimate transactions submitted by other users to be rejected. Repeated application across many parent transactions amplifies the effect.

### Likelihood Explanation

RBF is enabled by default when `min_rbf_rate > min_fee_rate` (the shipped `ckb.toml` sets `min_rbf_rate = 1_500` vs `min_fee_rate = 1_000`). [5](#0-4)  Any unprivileged peer can submit transactions via the `send_transaction` RPC. The two-step setup (submit parent + child, then RBF-replace child) requires no special privilege and is cheap to execute. The pool size is 180 MB by default, so the effect is most pronounced when the pool is under load, which is a realistic mainnet condition.

### Recommendation

In `remove_entry_and_descendants`, collect the direct parents of the root entry **before** stripping links, then explicitly call `sub_descendant_weight` and recompute `evict_key` for each surviving parent after all removals are complete. Alternatively, restructure the function so that `remove_entry_links` is called only after `update_ancestors_index_key` has already walked the live graph for the root entry.

### Proof of Concept

```
1. Node has RBF enabled (min_rbf_rate > min_fee_rate, default config).
2. Submit tx_A spending a confirmed UTXO, fee-rate just above min_fee_rate.
3. Submit tx_B spending tx_A's output, fee-rate well above min_rbf_rate.
   → pool_map: tx_A.descendants_count = 2, tx_A.evict_key reflects count=2.
4. Submit tx_C spending the same UTXO as tx_B with fee > tx_B.fee + rbf_extra.
   → process_rbf calls remove_entry_and_descendants(tx_B.short_id).
   → remove_entry_links clears tx_B from self.links.inner.
   → remove_entry calls update_ancestors_index_key(tx_B, Remove).
   → calc_ancestors(tx_B.short_id) returns {} because links are gone.
   → tx_A.descendants_count remains 2; tx_A.evict_key is NOT updated.
5. Fill the pool to capacity with many small transactions.
6. Submit one more transaction; limit_size fires.
   → next_evict_entry iterates by evict_key ascending.
   → tx_A's inflated evict_key (descendants_count=2) causes it to sort
      above a legitimate tx with descendants_count=1 and equal fee-rate.
   → The legitimate tx is evicted and receives Reject::Full instead of tx_A.
```

Relevant code locations:
- `remove_entry_and_descendants`: [6](#0-5) 
- `update_ancestors_index_key` (the skipped update): [2](#0-1) 
- `sub_descendant_weight` (the stale field): [7](#0-6) 
- `limit_size` eviction loop: [4](#0-3) 
- `process_rbf` calling `remove_entry_and_descendants`: [8](#0-7)

### Citations

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

**File:** resource/ckb.toml (L212-214)
```text
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
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

**File:** tx-pool/src/process.rs (L203-206)
```rust
        let all_removed: Vec<_> = conflicts
            .iter()
            .flat_map(|id| tx_pool.pool_map.remove_entry_and_descendants(id))
            .collect();
```
