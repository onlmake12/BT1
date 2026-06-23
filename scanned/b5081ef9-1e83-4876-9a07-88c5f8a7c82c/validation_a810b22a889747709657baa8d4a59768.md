### Title
O(N²) Resource Exhaustion in `PoolMap::remove_entry` via Unbounded `update_descendants_index_key` Loop During Mass Transaction Expiry — (`tx-pool/src/component/pool_map.rs`)

---

### Summary

`PoolMap::remove_entry` calls `update_descendants_index_key`, which performs a full graph traversal to collect and update all descendants of the removed transaction. When `remove_expired` removes all expired transactions in a chain sequentially within a single block-processing event, the cumulative work is O(N²) in the chain length N. This holds the tx-pool write lock for the entire duration, blocking all concurrent tx-pool operations.

---

### Finding Description

`remove_entry` in `tx-pool/src/component/pool_map.rs` performs two unbounded traversals on every call:

```rust
pub(crate) fn remove_entry(&mut self, id: &ProposalShortId) -> Option<TxEntry> {
    self.entries.remove_by_id(id).map(|entry| {
        self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
        self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
        self.remove_entry_edges(&entry.inner);
        self.remove_entry_links(id);   // ← links removed AFTER the traversals
        ...
    })
}
``` [1](#0-0) 

`update_descendants_index_key` calls `calc_descendants`, which performs a full BFS/DFS over the link graph to collect every descendant, then iterates over all of them:

```rust
fn update_descendants_index_key(&mut self, parent: &TxEntry, op: EntryOp) {
    let descendants: HashSet<ProposalShortId> =
        self.links.calc_descendants(&parent.proposal_short_id());
    for desc_id in &descendants {
        self.entries.modify_by_id(desc_id, |e| { ... });
    }
}
``` [2](#0-1) 

`calc_descendants` itself is an unbounded BFS with no iteration cap: [3](#0-2) 

Critically, `remove_entry_links` is called **after** `update_descendants_index_key`, so the link graph is still fully intact when the traversal runs. This means:

- When the root of a chain of N transactions is removed, `update_descendants_index_key` traverses all N−1 descendants.
- When the next transaction in the chain is removed, it traverses N−2 descendants.
- Total work: (N−1) + (N−2) + … + 1 = **O(N²/2)**.

`remove_expired` removes all expired transactions in a single sequential loop without batching or pagination:

```rust
pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
    let removed: Vec<_> = self.pool_map.iter()
        .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
        .map(|entry| entry.inner.clone())
        .collect();

    for entry in removed {
        self.pool_map.remove_entry(&entry.proposal_short_id());
        ...
    }
}
``` [4](#0-3) 

`remove_expired` is called from `_update_tx_pool_for_reorg` on every block, while holding the tx-pool write lock: [5](#0-4) 

The default `max_ancestors_count` is 1,000, meaning a chain can be up to 1,000 transactions deep: [6](#0-5) 

With the default pool size of 180 MB and a minimum transaction size of ~200 bytes, an attacker can submit approximately 900 independent chains of 1,000 transactions each. When all expire simultaneously, `remove_expired` performs approximately 900 × (1,000²/2) ≈ **450,000,000 descendant-update operations** in a single block-processing event, all while holding the tx-pool write lock.

---

### Impact Explanation

The tx-pool write lock is held throughout `_update_tx_pool_for_reorg` → `remove_expired`. During this period:

- No new transactions can be submitted or verified.
- No block templates can be generated (miner stall).
- All RPC calls requiring the tx-pool write lock are blocked.

The node becomes effectively unresponsive to tx-pool operations for the duration of the O(N²) work. With 450 million operations, this can take several seconds on commodity hardware, causing observable liveness degradation.

---

### Likelihood Explanation

**Low.** The attacker must:
1. Submit enough transactions to fill chains up to `max_ancestors_count` depth (requires paying fees and consuming bandwidth).
2. Wait the default 12-hour expiry window (`expiry_hours = 12`) before the effect triggers.
3. The attack is self-limiting: once the pool is full (180 MB), no more transactions are accepted.

The attack does not require miner cooperation, privileged access, or a leaked key — any tx-pool submitter reachable via the standard `send_transaction` RPC can execute it.

---

### Recommendation

1. **Batch or paginate `remove_expired`**: Instead of removing all expired transactions in one call, remove them in bounded batches per block (e.g., at most 256 per block).
2. **Pre-remove links before iterating**: In `remove_expired`, call `remove_entry_links` for all expired entries before calling `remove_entry`, so that `update_descendants_index_key` finds an empty descendant set (similar to the existing optimization in `remove_entry_and_descendants`).
3. **Cap descendants per removal**: Add a guard in `update_descendants_index_key` that skips the update if the descendant count exceeds a threshold, relying on lazy recomputation instead.

---

### Proof of Concept

**Setup:**
1. Configure a CKB node with default settings (`max_ancestors_count = 1000`, `expiry_hours = 12`, `max_tx_pool_size = 180_000_000`).
2. Submit a root transaction `tx_root` spending a confirmed cell.
3. Submit 999 child transactions forming a chain: `tx_1 → tx_2 → … → tx_999`, each spending the output of the previous.
4. Repeat step 2–3 for as many independent chains as the pool size allows (~900 chains).
5. Wait 12 hours.

**Trigger:**
- The next block arrives and triggers `_update_tx_pool_for_reorg` → `remove_expired`.
- `remove_expired` collects all ~900,000 expired transactions and removes them sequentially.
- For each chain of 1,000 transactions, `remove_entry` → `update_descendants_index_key` → `calc_descendants` traverses the remaining descendants: 999 + 998 + … + 1 = 499,500 traversal steps per chain.
- Total: ~900 × 499,500 ≈ 450,000,000 operations under the tx-pool write lock.

**Observed effect:**
- The node's tx-pool is unresponsive (write-locked) for several seconds during block processing.
- `send_transaction`, `get_block_template`, and all write-path RPC calls time out or queue indefinitely during this window.

**Relevant code path:** [2](#0-1) [4](#0-3) [7](#0-6)

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

**File:** tx-pool/src/pool.rs (L271-288)
```rust
    pub(crate) fn remove_expired(&mut self, callbacks: &Callbacks) {
        let now_ms = ckb_systemtime::unix_time_as_millis();

        let removed: Vec<_> = self
            .pool_map
            .iter()
            .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
            .map(|entry| entry.inner.clone())
            .collect();

        for entry in removed {
            let tx_hash = entry.transaction().hash();
            debug!("remove_expired {} timestamp({})", tx_hash, entry.timestamp);
            self.pool_map.remove_entry(&entry.proposal_short_id());
            let reject = Reject::Expiry(entry.timestamp);
            callbacks.call_reject(self, &entry, reject);
        }
    }
```

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

**File:** util/app-config/src/legacy/tx_pool.rs (L16-16)
```rust
const DEFAULT_MAX_ANCESTORS_COUNT: usize = 1_000;
```
