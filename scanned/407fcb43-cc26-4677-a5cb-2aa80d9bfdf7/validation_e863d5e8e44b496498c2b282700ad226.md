### Title
Unbounded Iteration Over All Pending/Gap Pool Entries on Every Block — (`File: tx-pool/src/process.rs`)

### Summary
`_update_tx_pool_for_reorg` iterates over the entire pending and gap tx-pool without any per-call element count guard. An unprivileged attacker who floods the pool with minimum-size transactions forces this O(N) scan to run on every block, stalling the tx-pool service and degrading block-processing throughput.

### Finding Description
`_update_tx_pool_for_reorg` is invoked on every new block to reconcile the tx-pool with the new chain tip. When `mine_mode` is active it performs two full linear scans of the pool:

```
// tx-pool/src/process.rs  lines 1065-1080
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
    let short_id = entry.inner.proposal_short_id();
    if snapshot.proposals().contains_proposed(&short_id) {
        proposals.push((short_id, entry.inner.clone()));
    }
}

for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
    let short_id = entry.inner.proposal_short_id();
    let elem = (short_id.clone(), entry.inner.clone());
    if snapshot.proposals().contains_proposed(&short_id) {
        proposals.push(elem);
    } else if snapshot.proposals().contains_gap(&short_id) {
        gaps.push(elem);
    }
}
``` [1](#0-0) 

Neither loop has an early-exit or a per-invocation element limit. The pool is bounded only by `max_tx_pool_size` (default 180 MB). With the minimum serialised CKB transaction size of roughly 200 bytes, the pool can hold on the order of 900 000 entries. Every block triggers both scans in full.

A secondary, related surface exists in `pool_map.rs`. When any entry is added or removed, `update_descendants_index_key` and `update_ancestors_index_key` each perform an unbounded BFS over the full descendant/ancestor sub-graph via `calc_relation_ids`:

```
// tx-pool/src/component/links.rs  lines 52-72
pub fn calc_relation_ids(
    &self,
    mut stage: HashSet<ProposalShortId>,
    relation: Relation,
) -> HashSet<ProposalShortId> {
    let mut relation_ids = HashSet::with_capacity(stage.len());
    while let Some(id) = stage.iter().next().cloned() {
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
``` [2](#0-1) 

`max_ancestors_count` caps the *depth* of any single chain but does not cap the *width* of the descendant tree. A transaction with N outputs can have N direct children, each with N children, producing O(N^depth) descendants bounded only by pool capacity. [3](#0-2) [4](#0-3) 

### Impact Explanation
Every block causes `_update_tx_pool_for_reorg` to scan the entire pool. With a pool filled to capacity the scan dominates the block-processing critical path. The tx-pool service holds a write lock during this operation; all concurrent RPC calls and relay handlers that need the pool are blocked for the duration. Sustained block arrival (one every ~10 s on mainnet) with a maximally filled pool can keep the node perpetually behind tip, effectively a liveness DoS for mining nodes and a relay-latency DoS for full nodes.

### Likelihood Explanation
The attack requires only the ability to submit transactions via the public `send_transaction` RPC or the P2P relay protocol — both are available to any unprivileged peer. Filling the pool to 180 MB with minimum-fee transactions is cheap on a low-fee network. No special privilege, key material, or majority hashpower is required.

### Recommendation
1. **Cap the per-block promotion scan**: in `_update_tx_pool_for_reorg`, add a configurable `MAX_ENTRIES_PER_REORG_SCAN` constant and break out of the pending/gap loops once that limit is reached, deferring remaining work to the next block.
2. **Bound descendant/ancestor traversal**: in `calc_relation_ids`, accept an optional `max_depth` or `max_count` parameter and return an error or truncated set when the limit is exceeded, mirroring the existing `max_ancestors_count` guard in `check_and_record_ancestors`.
3. **Enforce a maximum entry count** (not just a byte-size limit) in the pool so that the number of iterations is predictable regardless of transaction size distribution.

### Proof of Concept
1. Connect to a CKB node with `mine_mode` enabled (any node running a block assembler).
2. Submit ~900 000 minimum-size, independent transactions via `send_transaction` until `total_tx_size` approaches `max_tx_pool_size` (180 MB).
3. Observe via metrics or logs that each subsequent block causes `_update_tx_pool_for_reorg` to iterate over all ~900 000 entries, holding the tx-pool write lock for an extended period.
4. Confirm that RPC latency for pool-touching calls (`get_transaction`, `send_transaction`) spikes on every block arrival, and that the node's tip lags behind the network tip. [5](#0-4) [6](#0-5)

### Citations

**File:** tx-pool/src/process.rs (L1039-1114)
```rust
fn _update_tx_pool_for_reorg(
    tx_pool: &mut TxPool,
    attached: &LinkedHashSet<TransactionView>,
    detached_headers: &HashSet<Byte32>,
    detached_proposal_id: HashSet<ProposalShortId>,
    snapshot: Arc<Snapshot>,
    callbacks: &Callbacks,
    mine_mode: bool,
) {
    tx_pool.snapshot = Arc::clone(&snapshot);

    // NOTE: `remove_by_detached_proposal` will try to re-put the given expired/detached proposals into
    // pending-pool if they can be found within txpool. As for a transaction
    // which is both expired and committed at the one time(commit at its end of commit-window),
    // we should treat it as a committed and not re-put into pending-pool. So we should ensure
    // that involves `remove_committed_txs` before `remove_expired`.
    tx_pool.remove_committed_txs(attached.iter(), callbacks, detached_headers);
    tx_pool.remove_by_detached_proposal(detached_proposal_id.iter());

    // mine mode:
    // pending ---> gap ----> proposed
    // try move gap to proposed
    if mine_mode {
        let mut proposals = Vec::new();
        let mut gaps = Vec::new();

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) {
            let short_id = entry.inner.proposal_short_id();
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push((short_id, entry.inner.clone()));
            }
        }

        for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) {
            let short_id = entry.inner.proposal_short_id();
            let elem = (short_id.clone(), entry.inner.clone());
            if snapshot.proposals().contains_proposed(&short_id) {
                proposals.push(elem);
            } else if snapshot.proposals().contains_gap(&short_id) {
                gaps.push(elem);
            }
        }

        for (id, entry) in proposals {
            debug!("begin to proposed: {:x}", id);
            if let Err(e) = tx_pool.proposed_rtx(&id) {
                debug!(
                    "Failed to add proposed tx {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e);
            } else {
                callbacks.call_proposed(&entry)
            }
        }

        for (id, entry) in gaps {
            debug!("begin to gap: {:x}", id);
            if let Err(e) = tx_pool.gap_rtx(&id) {
                debug!(
                    "Failed to add tx to gap {}, reason: {}",
                    entry.transaction().hash(),
                    e
                );
                callbacks.call_reject(tx_pool, &entry, e.clone());
            }
        }
    }

    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
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

**File:** tx-pool/src/component/pool_map.rs (L77-90)
```rust
impl PoolMap {
    pub fn new(max_ancestors_count: usize) -> Self {
        PoolMap {
            entries: MultiIndexPoolEntryMap::default(),
            edges: Edges::default(),
            links: TxLinksMap::new(),
            max_ancestors_count,
            total_tx_size: 0,
            total_tx_cycles: 0,
            pending_count: 0,
            gap_count: 0,
            proposed_count: 0,
        }
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
