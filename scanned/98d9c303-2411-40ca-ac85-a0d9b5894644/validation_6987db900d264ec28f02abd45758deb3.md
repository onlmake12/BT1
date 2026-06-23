### Title
Stale Ancestor Descendant-Weight Statistics After Batch Removal Causes Incorrect Eviction Scoring and Tx-Pool DoS - (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

In `PoolMap::remove_entry_and_descendants`, all parent-child links are cleared **before** `remove_entry` is called for each removed entry. Because `update_ancestors_index_key` resolves ancestors through the now-empty link map, the surviving ancestors of the removed subtree never have their `descendants_fee / descendants_size / descendants_cycles / descendants_count` fields decremented. Those fields are the sole inputs to `EvictKey`, which drives pool eviction order. An unprivileged transaction sender can exploit this to keep low-fee transactions in the pool indefinitely, crowding out legitimate transactions.

---

### Finding Description

`remove_entry_and_descendants` is the only batch-removal path in the pool. Its implementation is:

```
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // update links state for remove, so that we won't update_descendants_index_key in remove_entry
    for id in &removed_ids {
        self.remove_entry_links(id);          // ← clears ALL links first
    }

    removed_ids
        .iter()
        .filter_map(|id| self.remove_entry(id))   // ← then removes entries
        .collect()
}
``` [1](#0-0) 

`remove_entry_links` for the root entry `id` removes `id` from every parent's children set and removes `id` from the link map entirely: [2](#0-1) 

When `remove_entry` is subsequently called, it invokes `update_ancestors_index_key`: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`. Because the link for `id` was already erased, `calc_ancestors` returns an empty set. The loop body — which calls `sub_descendant_weight` on each surviving ancestor — never executes. The surviving ancestors' `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` fields are permanently stale (inflated).

The `EvictKey` for each surviving ancestor is computed from those stale fields: [4](#0-3) 

`EvictKey.fee_rate` is `max(descendants_feerate, own_feerate)`. With inflated `descendants_fee` and `descendants_size`, `descendants_feerate` is artificially high, so the ancestor's evict key is artificially high, making it appear more valuable than it is and suppressing its eviction.

The `sub_descendant_weight` function that should have been called: [5](#0-4) 

---

### Impact Explanation

An attacker who controls two transactions — a low-fee parent A and a high-fee child B — can trigger removal of B (via RBF conflict, header-dep invalidation, or size-limit eviction) while leaving A in the pool. After removal, A retains B's fee contribution in its `descendants_fee`, making A's `EvictKey` appear high. A is therefore never selected by `next_evict_entry` even when the pool is full. By repeating this pattern the attacker fills the pool with low-fee transactions that carry phantom descendant fees, causing legitimate high-fee transactions to be rejected with `Reject::Full`.

The `limit_size` eviction loop: [6](#0-5) 

iterates `next_evict_entry` which selects the entry with the lowest `EvictKey`. Entries with stale (inflated) evict keys are skipped, so the pool never shrinks below the size limit through normal eviction, and new submissions are rejected.

---

### Likelihood Explanation

The attack is reachable by any unprivileged RPC caller or P2P peer that can submit transactions. `remove_entry_and_descendants` is triggered by ordinary conflict resolution (`resolve_conflict`, `resolve_conflict_header_dep`) and size-limit eviction (`limit_size`), all of which are reachable without any privileged access. The attacker pays fees for the parent and child transactions; the child can be displaced cheaply via a conflicting transaction that meets the minimum RBF fee bump. The stale state persists until the parent is committed or the node restarts.

---

### Recommendation

Compute the set of surviving ancestors **before** clearing any links, then apply `sub_descendant_weight` to each of them. Concretely, in `remove_entry_and_descendants`, collect the ancestors of the root entry prior to calling `remove_entry_links`, and update their descendant-weight fields and evict keys directly. Alternatively, restructure the function so that `remove_entry_links` is called only for the descendants (not the root), allowing `remove_entry`'s existing `update_ancestors_index_key` call to find and update the root's surviving ancestors correctly before the root's own links are cleared.

---

### Proof of Concept

```
Setup:
  A: low-fee tx (fee = 100 shannons, size = 200 bytes)
  B: child of A (fee = 10_000 shannons, size = 200 bytes)

After adding both:
  A.descendants_fee   = 10_100 shannons
  A.descendants_size  = 400 bytes
  A.descendants_feerate ≈ 25_250 shannons/kB  (high)
  A.evict_key.fee_rate = 25_250               (high → not evicted)

Attacker submits C, which spends the same input as B (conflict).
resolve_conflict calls remove_entry_and_descendants(B).
  remove_entry_links(B) clears B from A's children and from the link map.
  remove_entry(B) calls update_ancestors_index_key(B, Remove):
    calc_ancestors(B) → {} (link already gone)
    → A.sub_descendant_weight(B) is NEVER called

After removal:
  A.descendants_fee   = 10_100 shannons  ← should be 100
  A.descendants_size  = 400 bytes        ← should be 200
  A.evict_key.fee_rate = 25_250          ← should be 500 shannons/kB

Pool is full. next_evict_entry skips A (high evict key).
Legitimate tx D (fee = 5_000 shannons) is rejected with Reject::Full.
```

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
