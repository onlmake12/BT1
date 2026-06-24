Audit Report

## Title
O(N²) Ancestor BFS Re-traversal in `remove_by_detached_proposal` During Reorg — (`tx-pool/src/pool.rs`, `tx-pool/src/component/pool_map.rs`)

## Summary

`TxPool::remove_by_detached_proposal` re-inserts each removed entry via `add_pending`, which triggers two full ancestor BFS traversals per insertion through `check_and_record_ancestors` and `update_ancestors_index_key`. For a linear chain of N transactions re-inserted in ancestor-count order, this produces O(N²) total BFS work while holding the tx-pool write lock, blocking all concurrent tx-pool operations for the duration.

## Finding Description

**Confirmed code path:**

`remove_by_detached_proposal` (pool.rs L333–356) iterates over detached proposal IDs, calls `remove_entry_and_descendants`, sorts removed entries by `ancestors_count` (root first), then calls `add_pending(entry)` for each:

```rust
let mut entries = self.pool_map.remove_entry_and_descendants(id);
entries.sort_unstable_by_key(|entry| entry.ancestors_count);
for mut entry in entries {
    entry.reset_statistic_state();
    let ret = self.add_pending(entry);
```

`add_pending` (pool.rs L131–136) calls `pool_map.add_entry(entry, Status::Pending)`.

`add_entry` (pool_map.rs L200–221) performs two operations that each traverse all current ancestors:

- **Traversal 1** (line 213): `check_and_record_ancestors` → `get_tx_ancenstors` → `calc_relation_ids` — a BFS over all ancestors currently in the pool.
- **Traversal 2** (line 216): `record_entry_descendants` → `update_ancestors_index_key` (pool_map.rs L512) — another full BFS via `calc_ancestors` → `calc_relative_ids` → `calc_relation_ids`.

`update_ancestors_index_key` (pool_map.rs L432–444) is called **unconditionally** at line 512, outside the `if !children.is_empty()` guard, regardless of whether children exist.

`calc_relation_ids` (links.rs L52–72) is a BFS that visits every ancestor node.

When re-inserting in ancestor-count order (root first), when inserting the k-th transaction, exactly k−1 ancestors are already in the pool. Each of the two BFS traversals visits k−1 nodes. Note: `update_descendants_index_key` is NOT triggered during root-first re-insertion because children haven't been re-inserted yet and their inputs are not yet registered in `edges`.

```
Total work = 2 × Σ(k=0..N-1) k = N(N-1) = O(N²)
```

For N = 1000 (`max_ancestors_count`): ~999,000 BFS node visits, all while holding the tx-pool write lock.

**Existing guards are insufficient:** The `max_ancestors_count` limit (default 1000) bounds N but does not prevent the quadratic accumulation — it merely caps the maximum work at ~10⁶ operations per reorg event.

**Trigger path:** `_update_tx_pool_for_reorg` acquires the tx-pool write lock and calls `remove_by_detached_proposal` while holding it for the entire duration.

## Impact Explanation

While the tx-pool write lock is held, all concurrent operations are blocked: transaction submission, block template generation, and peer sync. For N=1000, ~10⁶ HashSet BFS operations execute in a single lock-holding call. This stalls reorg processing and delays block template generation. The impact maps to **Low (501–2000 points): Any other important performance improvements for CKB**. Escalation to High is not justified: the stall duration is bounded (milliseconds to low seconds for N=1000 in-memory HashSet operations), and a single brief stall on one node does not constitute network congestion.

## Likelihood Explanation

An unprivileged attacker needs only:
1. Submit a 1000-tx chain (allowed by protocol; fees required at minimum fee rate).
2. Wait for a block that proposes the short IDs (miners include proposals automatically).
3. A 1-block reorg occurs — natural reorgs occur on mainnet; the attacker does not need to control hashpower, but cannot reliably force a reorg without it.

The attack is repeatable after each qualifying reorg event. Cost is bounded to 1000 transaction fees.

## Recommendation

Replace per-insertion ancestor BFS with a single-pass bulk re-insertion:

1. After `remove_entry_and_descendants`, re-insert all entries in topological order (already done via `ancestors_count` sort).
2. For each re-inserted entry, compute ancestors incrementally from the already-inserted parent set using O(1) parent lookups rather than running a full BFS.
3. Defer `update_ancestors_index_key` and `_record_ancestors` calls until the full chain is re-linked, then perform a single batch update of ancestor/descendant weights, reducing total complexity from O(N²) to O(N·E) where E is the average number of edges per node (typically 1 for a chain).

Alternatively, add a dedicated `bulk_add_pending` path in `PoolMap` that accepts a pre-sorted slice of entries and builds the link graph in a single pass without repeated BFS.

## Proof of Concept

1. Build tx0 (coinbase spend), tx1 (spends tx0 output), …, tx999 (spends tx998 output).
2. Submit all 1000 transactions to the node via RPC/P2P.
3. Wait for a block that proposes all 1000 short IDs.
4. Trigger a 1-block reorg (e.g., by broadcasting a competing block at the same height with equal or greater work).
5. Observe: `_update_tx_pool_for_reorg` calls `remove_by_detached_proposal` with 1000 IDs.
6. Benchmark `remove_by_detached_proposal` with chains of length 100, 200, 500, 1000 and plot wall-clock time — quadratic growth (100× slower for 10× more transactions) is directly verifiable.
7. During the stall, confirm that concurrent RPC calls for tx submission and `get_block_template` are blocked until the lock is released.

A unit benchmark can be written directly against `PoolMap` by calling `add_entry` with `Status::Proposed` for a chain of N transactions, then calling `remove_entry_and_descendants` followed by re-insertion via `add_entry(Status::Pending)` in sorted order, timing the re-insertion loop for N ∈ {100, 200, 500, 1000}. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/pool.rs (L131-136)
```rust
    pub(crate) fn add_pending(
        &mut self,
        entry: TxEntry,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        self.pool_map.add_entry(entry, Status::Pending)
    }
```

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

**File:** tx-pool/src/component/pool_map.rs (L200-221)
```rust
    pub(crate) fn add_entry(
        &mut self,
        mut entry: TxEntry,
        status: Status,
    ) -> Result<(bool, HashSet<TxEntry>), Reject> {
        let tx_short_id = entry.proposal_short_id();
        let mut evicts = Default::default();
        if self.entries.get_by_id(&tx_short_id).is_some() {
            return Ok((false, evicts));
        }
        let (total_tx_size, total_tx_cycles) =
            self.updated_stat_for_add_tx(entry.size, entry.cycles)?;
        trace!("pool_map.add_{:?} {}", status, entry.transaction().hash());
        evicts = self.check_and_record_ancestors(&mut entry)?;
        self.record_entry_edges(&entry)?;
        self.insert_entry(&entry, status);
        self.record_entry_descendants(&entry);
        self.track_entry_statics(None, Some(status));
        self.total_tx_size = total_tx_size;
        self.total_tx_cycles = total_tx_cycles;
        Ok((true, evicts))
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

**File:** tx-pool/src/component/pool_map.rs (L487-513)
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
        // update ancestor's index key for adding new entry
        self.update_ancestors_index_key(entry, EntryOp::Add);
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
