Let me verify the key code paths in detail.

Audit Report

## Title
Stale Ancestor `descendants_fee` Accounting After Subtree Removal Enables Pool Eviction Manipulation - (File: `tx-pool/src/component/pool_map.rs`)

## Summary
`remove_entry_and_descendants` pre-removes all link records for the entire subtree — including the root — before calling `remove_entry` on each node. When `remove_entry` subsequently calls `update_ancestors_index_key` for the root, `calc_ancestors` returns an empty set because the root's link record was already deleted. Surviving ancestors of the removed root never receive `sub_descendant_weight`, leaving their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` permanently inflated. An unprivileged attacker can exploit this via repeated RBF replacements to make a low-fee parent transaction immune to eviction, fill the pool, and cause legitimate transactions to be rejected with `Reject::Full`.

## Finding Description

`remove_entry_and_descendants` collects the root and all its descendants, then calls `remove_entry_links` for every node in the set — including the root — before calling `remove_entry` on any of them: [1](#0-0) 

`remove_entry_links` removes the node's own entry from `links.inner` via `self.links.remove(id)`: [2](#0-1) 

When `remove_entry` is subsequently called for the root, it calls `update_ancestors_index_key`: [3](#0-2) 

`update_ancestors_index_key` calls `self.links.calc_ancestors(&child.proposal_short_id())`: [4](#0-3) 

`calc_ancestors` delegates to `calc_relative_ids`, which first looks up the node's own link entry in `self.links.inner`: [5](#0-4) 

Because `remove_entry_links` already called `self.links.remove(id)` for the root, `self.inner.get(short_id)` returns `None`, `direct` is an empty set, and `calc_relation_ids` returns an empty set. No surviving ancestor ever receives `sub_descendant_weight`. Their `descendants_fee`, `descendants_size`, `descendants_cycles`, and `descendants_count` remain permanently inflated.

**Concrete trace for P → C1, then RBF with C1':**

1. Before removal: `links.inner = {P: {parents:{}, children:{C1}}, C1: {parents:{P}, children:{}}}`; `P.descendants_fee = P.fee + C1.fee`.
2. `remove_entry_links(C1_id)` runs: removes C1 from P's children set, then calls `links.remove(C1_id)` — C1's link record is gone. `links.inner = {P: {parents:{}, children:{}}}`.
3. `remove_entry(C1_id)` runs → `update_ancestors_index_key(C1, Remove)` → `calc_ancestors(C1_id)` → `self.inner.get(C1_id)` returns `None` → ancestors = `{}` → **P's `descendants_fee` is not decremented**.
4. C1' is added as new child of P → `P.descendants_fee += C1'.fee`.
5. After one replacement: `P.descendants_fee = P.fee + C1.fee (stale) + C1'.fee`.

The stale `descendants_fee` feeds directly into `EvictKey` computation: [6](#0-5) 

`resolve_conflict` — the RBF path — calls `remove_entry_and_descendants` and is reachable by any unprivileged caller via `send_transaction`: [7](#0-6) 

The size-limit eviction loop also calls `remove_entry_and_descendants`, compounding the effect: [8](#0-7) 

## Impact Explanation

A surviving ancestor P retains an inflated `descendants_feerate`. Its `EvictKey.fee_rate` is `descendants_feerate.max(feerate)`, so with an artificially high `descendants_feerate` it ranks as highly valuable and is never selected by `next_evict_entry` (which iterates `iter_by_evict_key()` in ascending order). An attacker can keep a near-zero-fee parent permanently in the pool and fill the pool with such entries, causing legitimate transactions to be rejected with `Reject::Full`. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs.**

## Likelihood Explanation

Any node's RPC endpoint accepts `send_transaction` from unprivileged callers. RBF is enabled whenever `min_rbf_rate > min_fee_rate`. Each replacement requires paying a marginally higher fee for the child, but permanently inflates the parent's `descendants_fee` by the replaced child's fee. After N replacements the inflation is `sum(child_fees[0..N-1])`, growing without bound. No privileged access, key material, or majority hashpower is required. The cost per inflation step is bounded only by the RBF fee increment, which can be set to the minimum allowed.

## Recommendation

Before calling `remove_entry_links` for the subtree root in `remove_entry_and_descendants`, compute `calc_ancestors(id)` for the root while its link record still exists, then iterate over those ancestor IDs and call `sub_descendant_weight` with the root entry's weight. This mirrors the logic in `update_ancestors_index_key` but executes it before links are torn down:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));

    // Fix: update surviving ancestors of the root BEFORE removing links
    if let Some(root_entry) = self.entries.get_by_id(id).map(|e| e.inner.clone()) {
        let ancestors = self.links.calc_ancestors(id);
        for anc_id in &ancestors {
            if !removed_ids.contains(anc_id) {
                self.entries.modify_by_id(anc_id, |e| {
                    e.inner.sub_descendant_weight(&root_entry);
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

## Proof of Concept

1. Add parent transaction P (low fee) to the pool.
2. Add child transaction C1 (high fee, spends P's output). Now `P.descendants_fee = P.fee + C1.fee`.
3. Submit C1' (higher fee, same input as C1) via `send_transaction`. RBF triggers `resolve_conflict` → `remove_entry_and_descendants(C1_id)`:
   - `remove_entry_links(C1_id)` severs P→C1 link and removes C1's link record.
   - `remove_entry(C1_id)` → `update_ancestors_index_key(C1, Remove)` → `calc_ancestors(C1_id)` returns `{}` (link gone) → P's `descendants_fee` is NOT decremented.
4. C1' is added as new child of P → `P.descendants_fee += C1'.fee`.
5. After one replacement: `P.descendants_fee = P.fee + C1.fee (stale) + C1'.fee`.
6. Repeat steps 3–4 with C2, C3, … After N replacements: `P.descendants_fee = P.fee + sum(C1..CN fees) + CN'.fee`.
7. P's `EvictKey` reflects this inflated value; P is never selected for eviction.
8. Fill the pool with such entries; legitimate transactions receive `Reject::Full`.

A unit test can be written directly against `PoolMap`: add P, add C1, call `remove_entry_and_descendants(C1_id)`, then assert `pool_map.get(&P_id).unwrap().descendants_fee == P.fee` — this assertion will fail on the current code, confirming the bug.

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
