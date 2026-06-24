Audit Report

## Title
Unbounded Descendant Traversal in `remove_entry` Causes O(N) CPU Work on Block-Commit — (File: `tx-pool/src/component/pool_map.rs`)

## Summary
The CKB tx-pool enforces `max_ancestors_count` on insertion but imposes no symmetric limit on descendants. When a committed transaction is removed via `remove_committed_tx`, the pool calls `pool_map.remove_entry`, which unconditionally invokes `update_descendants_index_key`. That function performs a full BFS over all transitive descendants and calls `modify_by_id` for each one. Because `cell_dep` references create parent-child links in `TxLinksMap` with no descendant cap, an attacker can pre-load N descendants onto a single root transaction, causing O(N) CPU work on every block that commits that root.

## Finding Description

**Commit path — `remove_committed_tx` → `pool_map.remove_entry`:**

`remove_committed_tx` in `tx-pool/src/pool.rs` calls `pool_map.remove_entry` directly for each committed transaction: [1](#0-0) 

Inside `remove_entry`, `update_descendants_index_key` is called unconditionally before links are cleared: [2](#0-1) 

`update_descendants_index_key` computes the full transitive descendant set via BFS and calls `modify_by_id` for every entry: [3](#0-2) 

The BFS in `calc_relation_ids` is unbounded — it terminates only when no new children are found: [4](#0-3) 

**Why `remove_entry_and_descendants` does not apply:**

`remove_entry_and_descendants` pre-clears all links before calling `remove_entry`, making the inner traversal O(1). But committed transactions must keep their descendants in the pool, so they go through the plain `remove_entry` path, which does not pre-clear links: [5](#0-4) 

**How `cell_dep` references create parent-child links:**

`get_tx_ancenstors` adds any in-pool transaction referenced as a `cell_dep` to the `parents` set: [6](#0-5) 

`_record_ancestors` then calls `links.add_child(parent, short_id)` for each parent, establishing the link: [7](#0-6) 

`record_entry_descendants` also picks up existing cell-dep children via `edges.get_deps_ref`: [8](#0-7) 

**No descendant limit exists:**

Only `max_ancestors_count` is checked at insertion time: [9](#0-8) 

There is no corresponding `max_descendants_count` check anywhere in the codebase.

**Integration test confirms the attack surface:**

The existing test `TxPoolLimitAncestorCount` explicitly submits 2,000 transactions all referencing the same `cell_dep` parent and asserts they are all accepted: [10](#0-9) 

The comment at line 91 reads: *"we can have more than config.max_ancestors_count number of txs using one cell ref"* — confirming the absence of any descendant cap.

## Impact Explanation

**Impact: High — bad design that can cause CKB network congestion with low cost.**

The tx-pool service thread is single-threaded for write operations. An O(N) stall inside `remove_committed_tx` blocks all subsequent pool operations: transaction submission, block-template generation, and relay. With the default 180 MB pool and minimal transaction sizes, N can reach tens of thousands. A sustained attack (attacker continuously refills descendants after each committed root) keeps the pool thread near 100% CPU, degrading block propagation latency and potentially causing the node to fall behind the chain tip. This matches the allowed impact: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."*

## Likelihood Explanation

The attacker must pay fees proportional to N, but the minimum fee rate is operator-configurable and can be very low. No privileged access, mining power, or special network position is required — only the ability to submit valid transactions via RPC or P2P relay. The attack is repeatable: after each committed root, the attacker submits a new root and refills descendants. The pool size is operator-configurable upward, amplifying the attack on nodes with large pools.

## Recommendation

1. **Add a `max_descendants_count` limit** symmetric to `max_ancestors_count`. Enforce it in `record_entry_descendants` when a new child is linked to an existing parent, rejecting the child if the parent's transitive descendant count would exceed the limit.
2. **Alternatively**, cap `edges.deps[out_point].len()` — the number of in-pool transactions that may reference the same `out_point` as a `cell_dep`.
3. **Short-term mitigation**: increase `min_fee_rate` to raise the cost of filling the pool with fan-out descendants, or reduce `max_tx_pool_size`.

## Proof of Concept

```
1. Submit tx_root via send_transaction RPC (any valid tx, fee >= min_fee_rate).
2. For i in 1..N:
     Submit tx_i with:
       - inputs: [some unrelated live cell_i]
       - cell_deps: [OutPoint { tx_hash: tx_root.hash(), index: 0 }]
     Each tx_i has ancestors_count = 2 (tx_root + self), accepted by the pool.
     tx_root accumulates N children in TxLinksMap with no enforcement.
3. Mine a block containing tx_root.
4. Node calls remove_committed_txs → remove_committed_tx → pool_map.remove_entry(tx_root):
     → update_descendants_index_key performs BFS over all N descendants
     → N calls to modify_by_id on the multi-index map
     → O(N) CPU stall on the pool write thread
5. Repeat from step 1 with a new tx_root.
```

The existing integration test at `test/src/specs/tx_pool/limit.rs:93–100` already demonstrates that 2,000 such cell-dep transactions are accepted without error, providing a ready-made reproduction baseline. Extending it to measure wall-clock time for `remove_committed_tx` with N = 10,000 would produce a measurable stall.

### Citations

**File:** tx-pool/src/pool.rs (L253-257)
```rust
    fn remove_committed_tx(&mut self, tx: &TransactionView, callbacks: &Callbacks) {
        let short_id = tx.proposal_short_id();
        if let Some(_entry) = self.pool_map.remove_entry(&short_id) {
            debug!("remove_committed_tx for {}", tx.hash());
        }
```

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

**File:** tx-pool/src/component/pool_map.rs (L447-460)
```rust
    fn update_descendants_index_key(&mut self, parent: &TxEntry, op: EntryOp) {
        let descendants: HashSet<ProposalShortId> =
            self.links.calc_descendants(&parent.proposal_short_id());
        for desc_id in &descendants {
            // update child score
            self.entries.modify_by_id(desc_id, |e| {
                match op {
                    EntryOp::Remove => e.inner.sub_ancestor_weight(parent),
                    EntryOp::Add => e.inner.add_ancestor_weight(parent),
                };
                e.score = e.inner.as_score_key();
            });
        }
    }
```

**File:** tx-pool/src/component/pool_map.rs (L487-510)
```rust
    fn record_entry_descendants(&mut self, entry: &TxEntry) {
        let tx_short_id: ProposalShortId = entry.proposal_short_id();
        let outputs = entry.transaction().output_pts();
        let mut children = HashSet::new();

        // collect children
        for o in outputs {
            if let Some(ids) = self.edges.get_deps_ref(&o).cloned() {
                children.extend(ids);
            }
            if let Some(id) = self.edges.get_input_ref(&o).cloned() {
                children.insert(id);
            }
        }
        // update children
        if !children.is_empty() {
            for child in &children {
                self.links.add_parent(child, tx_short_id.clone());
            }
            if let Some(links) = self.links.inner.get_mut(&tx_short_id) {
                links.children.extend(children);
            }
            self.update_descendants_index_key(entry, EntryOp::Add);
        }
```

**File:** tx-pool/src/component/pool_map.rs (L541-547)
```rust
        for cell_dep in entry.cell_deps() {
            let dep_pt = cell_dep.out_point();
            let id = ProposalShortId::from_tx_hash(&dep_pt.tx_hash());
            if self.links.inner.contains_key(&id) {
                parents.insert(id);
            }
        }
```

**File:** tx-pool/src/component/pool_map.rs (L570-572)
```rust
        for parent in &parents {
            self.links.add_child(parent, short_id.clone());
        }
```

**File:** tx-pool/src/component/pool_map.rs (L595-601)
```rust
        let mut ancestors_count = ancestors.len() + 1;
        let mut evicted = Default::default();

        if ancestors_count <= self.max_ancestors_count {
            self._record_ancestors(entry, ancestors, parents);
            return Ok(evicted);
        }
```

**File:** tx-pool/src/component/links.rs (L52-72)
```rust
    pub fn calc_relation_ids(
        &self,
        mut stage: HashSet<ProposalShortId>,
        relation: Relation,
    ) -> HashSet<ProposalShortId> {
        let mut relation_ids = HashSet::with_capacity(stage.len());

        while let Some(id) = stage.iter().next().cloned() {
            //recursively
            if let Some(tx_links) = self.inner.get(&id) {
                for direct_id in tx_links.get_direct_ids(relation) {
                    if !relation_ids.contains(direct_id) {
                        stage.insert(direct_id.clone());
                    }
                }
            }
            stage.remove(&id);
            relation_ids.insert(id);
        }
        relation_ids
    }
```

**File:** test/src/specs/tx_pool/limit.rs (L90-101)
```rust
        // Create 250 transactions cell dep on tx_a
        // we can have more than config.max_ancestors_count number of txs using one cell ref
        let mut cell_ref_txs = vec![];
        for i in 1..=2000 {
            let cur = always_success_transaction(node0, initial_inputs.get(i).unwrap());
            let cur = cur.as_advanced_builder().cell_dep(cell_dep.clone()).build();
            let res = node0
                .rpc_client()
                .send_transaction_result(cur.data().into());
            assert!(res.is_ok());
            cell_ref_txs.push(cur.clone());
        }
```
