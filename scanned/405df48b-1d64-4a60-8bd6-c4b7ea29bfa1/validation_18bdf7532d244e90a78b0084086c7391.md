### Title
O(N²) Ancestor Re-traversal in `remove_by_detached_proposal` During Reorg — (`tx-pool/src/pool.rs`, `tx-pool/src/component/pool_map.rs`)

---

### Summary

`TxPool::remove_by_detached_proposal` removes a proposed transaction chain and re-inserts each entry into pending via `add_pending`. Each `add_pending` call triggers two full ancestor BFS traversals. For a linear chain of N transactions, this produces O(N²) total work. With `max_ancestors_count = 1000`, an unprivileged attacker can force ~1,000,000 BFS steps during any reorg that detaches the proposal window, stalling the tx-pool write lock.

---

### Finding Description

**Step 1 — `remove_by_detached_proposal` removes and re-inserts the chain** [1](#0-0) 

For each detached proposal ID, the function:
1. Calls `remove_entry_and_descendants(id)` — removes the root and all N descendants atomically.
2. Sorts the removed entries by `ancestors_count` (root first).
3. Calls `add_pending(entry)` for each of the N entries.

**Step 2 — `add_pending` → `add_entry` → two O(depth) BFS traversals per insertion**

`add_entry` performs two operations that each traverse all current ancestors:

*Traversal 1*: `check_and_record_ancestors` calls `get_tx_ancenstors`, which calls `calc_relation_ids` — a BFS over all ancestors in the pool: [2](#0-1) [3](#0-2) 

*Traversal 2*: `record_entry_descendants` calls `update_ancestors_index_key`, which calls `calc_ancestors` — another full BFS over all ancestors: [4](#0-3) 

**Step 3 — Quadratic accumulation**

Because entries are re-inserted in ancestor-count order (root first), when inserting the k-th transaction, exactly k−1 ancestors are already in the pool. Each of the two BFS traversals visits k−1 nodes. Total work:

```
2 × (0 + 1 + 2 + ... + (N-1)) = O(N²)
```

For N = 1000: ~1,000,000 BFS node visits, all while holding the tx-pool write lock.

**Step 4 — Trigger path (reorg)**

`remove_by_detached_proposal` is called from `_update_tx_pool_for_reorg` during every reorg: [5](#0-4) 

The reorg handler holds the tx-pool write lock for the entire duration: [6](#0-5) 

---

### Impact Explanation

While the tx-pool write lock is held, all concurrent operations are blocked: transaction submission, block template generation, and peer sync. A 1000-tx chain causes O(10⁶) HashSet BFS operations in a single lock-holding call. This stalls the node's reorg processing, delays chain sync, and can be repeated by the attacker after each reorg event.

---

### Likelihood Explanation

The attacker's requirements are:
1. **Submit a 1000-tx chain**: Allowed by the protocol; miners naturally pick up proposal short IDs from the pending pool and include them in blocks.
2. **Get the chain proposed**: No special privilege needed — miners include proposals automatically.
3. **Trigger a reorg**: Even a natural 1-block reorg (which occurs regularly on mainnet) is sufficient. The attacker does not need to control hashpower.

Cost: transaction fees for 1000 transactions. No privileged access required.

---

### Recommendation

Replace the per-insertion ancestor BFS with a single-pass bulk re-insertion:

1. After `remove_entry_and_descendants`, re-insert all entries in topological order (already done via `ancestors_count` sort).
2. For each re-inserted entry, compute ancestors incrementally from the already-inserted parent set (O(1) parent lookup) rather than running a full BFS.
3. Alternatively, batch-update ancestor/descendant weights after all entries are re-linked, avoiding repeated `calc_ancestors` / `calc_relation_ids` calls per entry.

The `_record_ancestors` and `update_ancestors_index_key` calls should be deferred until the full chain is re-linked, reducing total complexity from O(N²) to O(N·E) where E is the average number of edges per node (typically 1 for a chain).

---

### Proof of Concept

```
1. Build tx0 (coinbase spend), tx1 (spends tx0 output), ..., tx999 (spends tx998 output).
2. Submit all 1000 transactions to the node via RPC/P2P.
3. Wait for a block that proposes all 1000 short IDs (miners do this automatically).
4. Trigger a 1-block reorg (e.g., by broadcasting a competing block at the same height).
5. Observe: `_update_tx_pool_for_reorg` calls `remove_by_detached_proposal` with 1000 IDs.
6. Measure wall-clock time for the reorg handler; it will be quadratically longer than
   a 100-tx chain (expected ~100× slower for 10× more transactions).
7. During this period, all tx-pool operations (new tx submission, block template) are blocked.
```

The quadratic behavior is directly verifiable by benchmarking `remove_by_detached_proposal` with chains of length 100, 200, 500, 1000 and plotting wall-clock time.

### Citations

**File:** tx-pool/src/pool.rs (L333-356)
```rust
    pub(crate) fn remove_by_detached_proposal<'a>(
        &mut self,
        ids: impl Iterator<Item = &'a ProposalShortId>,
    ) {
        for id in ids {
            if let Some(e) = self.pool_map.get_by_id(id) {
                let status = e.status;
                if status == Status::Pending {
                    continue;
                }
                let mut entries = self.pool_map.remove_entry_and_descendants(id);
                entries.sort_unstable_by_key(|entry| entry.ancestors_count);
                for mut entry in entries {
                    let tx_hash = entry.transaction().hash();
                    entry.reset_statistic_state();
                    let ret = self.add_pending(entry);
                    debug!(
                        "remove_by_detached_proposal from {:?} {} add_pending {:?}",
                        status, tx_hash, ret
                    );
                }
            }
        }
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

**File:** tx-pool/src/component/pool_map.rs (L549-553)
```rust
        let ancestors = self
            .links
            .calc_relation_ids(parents.clone(), Relation::Parents);

        (ancestors, parents, cell_ref_parents)
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

**File:** tx-pool/src/process.rs (L836-851)
```rust
            let mut tx_pool = self.tx_pool.write().await;

            _update_tx_pool_for_reorg(
                &mut tx_pool,
                &attached,
                &detached_headers,
                detached_proposal_id,
                snapshot,
                &self.callbacks,
                mine_mode,
            );

            // notice: readd_detached_tx don't update cache
            self.readd_detached_tx(&mut tx_pool, retain, fetched_cache)
                .await;
        }
```

**File:** tx-pool/src/process.rs (L1055-1056)
```rust
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());
```
