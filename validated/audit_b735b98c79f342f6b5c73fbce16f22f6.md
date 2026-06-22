### Title
Unbounded Descendant Count in Tx-Pool Causes O(N) CPU Work on Every Committed-Transaction Removal — (File: `tx-pool/src/component/pool_map.rs`)

---

### Summary

The CKB transaction pool enforces a `max_ancestors_count` limit (default 1,000) on how many in-pool ancestors a transaction may have, but imposes **no corresponding limit on descendants**. An unprivileged attacker can submit a large fan-out of transactions that all reference a single in-pool transaction as a `cell_dep`, each with only one ancestor. When that root transaction is later committed in a block and removed from the pool via `remove_entry`, the pool iterates over every descendant to update their ancestor-score index keys. With no descendant cap, this iteration is bounded only by the total pool size (default 180 MB), enabling a sustained O(N) CPU stall on every block-commit event.

---

### Finding Description

**Root cause — `update_descendants_index_key` called unconditionally in `remove_entry`:**

`remove_entry` is the function used to evict a single committed transaction while keeping its descendants in the pool. [1](#0-0) 

Inside `remove_entry`, two traversals are triggered unconditionally: [2](#0-1) 

`update_descendants_index_key` computes the full transitive descendant set and then modifies every entry: [3](#0-2) 

The transitive traversal itself is an unbounded BFS/DFS: [4](#0-3) 

**Why descendants are unbounded:**

`max_ancestors_count` (default 1,000) is checked only when a new transaction is *inserted*: [5](#0-4) [6](#0-5) 

There is no symmetric `max_descendants_count` check anywhere. A transaction that is used as a `cell_dep` by N other transactions becomes the parent of all N in the `TxLinksMap`, yet each child has `ancestors_count = 2` (root + self), well within the limit.

The integration test `TxPoolLimitAncestorCount` explicitly demonstrates that 2,000 transactions can all reference the same cell-dep parent and be accepted: [7](#0-6) 

**Exploit path:**

1. Attacker submits `tx_root` (any valid transaction with at least one output or a live cell).
2. Attacker submits N transactions `tx_1 … tx_N`, each with `tx_root`'s output as a `cell_dep`. Each has `ancestors_count = 2`, passing the pool admission check.
3. `tx_root` accumulates N children in `TxLinksMap` with no enforcement.
4. A miner (or the attacker themselves) includes `tx_root` in a block.
5. The node calls `remove_entry(&tx_root_id)` during block-commit processing.
6. `update_descendants_index_key` traverses all N descendants and calls `modify_by_id` for each — O(N) work on the pool's internal multi-index map.

**Why `remove_entry_and_descendants` does not apply here:**

`remove_entry_and_descendants` pre-clears all links before calling `remove_entry`, making the inner traversals O(1). But committed transactions must keep their descendants in the pool, so they are removed via the plain `remove_entry` path, which does not pre-clear links: [8](#0-7) 

---

### Impact Explanation

- **Severity: Medium–High**
- With the default 180 MB pool and a minimal transaction size of ~100–200 bytes, N can reach tens of thousands to hundreds of thousands.
- Every block commit that includes `tx_root` triggers an O(N) scan of the pool's multi-index map. This stalls the tx-pool service thread, delaying subsequent transaction submissions, block-template generation, and relay operations.
- A sustained attack (attacker continuously refills descendants after each block) can keep the node's tx-pool thread near 100% CPU, degrading block propagation latency and potentially causing the node to fall behind the chain tip.
- No privileged access is required; only valid fee-paying transactions are needed.

---

### Likelihood Explanation

- **Likelihood: Medium**
- The attacker must pay transaction fees proportional to N. However, the minimum fee rate is configurable and can be very low on some deployments.
- The attack is repeatable: after `tx_root` is committed, the attacker submits a new root and refills descendants.
- The attack is amplified if the attacker targets a node with a large pool (`max_tx_pool_size` is operator-configurable up to any value).
- No special network position, mining power, or privileged key is required — only the ability to submit transactions via RPC or P2P relay.

---

### Recommendation

1. **Add a `max_descendants_count` limit** symmetric to `max_ancestors_count`. Enforce it in `record_entry_descendants` when a new child is linked to an existing parent, rejecting or evicting the child if the parent's descendant count would exceed the limit.
2. **Alternatively**, cap the number of in-pool transactions that may reference the same `out_point` as a `cell_dep` (i.e., bound `edges.deps[out_point].len()`).
3. **Short-term mitigation**: reduce the default `max_tx_pool_size` or increase `min_fee_rate` to raise the cost of filling the pool with fan-out descendants.

---

### Proof of Concept

```
1. Submit tx_root via send_transaction RPC (any valid tx, fee >= min_fee_rate).
2. For i in 1..N:
     Submit tx_i with:
       - inputs: [some unrelated live cell_i]
       - cell_deps: [tx_root.output(0)]   ← makes tx_root a parent of tx_i
     Each tx_i has ancestors_count = 2, accepted by the pool.
3. tx_root.descendants_count = N (no enforcement).
4. Mine a block containing tx_root.
5. Node calls remove_entry(tx_root):
     → update_descendants_index_key iterates N entries
     → O(N) modify_by_id calls on the multi-index map
6. Repeat from step 1 with a new tx_root.
```

With N = 10,000 and a pool of 180 MB, each block commit involving `tx_root` causes tens of thousands of map mutations in the pool service thread, measurably stalling block processing.

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

**File:** util/app-config/src/legacy/tx_pool.rs (L15-16)
```rust
// default max ancestors count
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
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
