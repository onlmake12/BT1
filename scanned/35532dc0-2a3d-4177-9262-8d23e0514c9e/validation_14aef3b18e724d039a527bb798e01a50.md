### Title
Unbounded Pool Iteration in `_update_tx_pool_for_reorg` Blocks tx-pool Write Lock on Every Block - (`tx-pool/src/process.rs`)

---

### Summary

`_update_tx_pool_for_reorg` iterates over **all** Gap and Pending entries in the tx-pool, and calls `remove_expired` which iterates over **all** pool entries, on every block reorg while holding the tx-pool write lock. The pool is bounded only by serialized byte size (`max_tx_pool_size`), not by transaction count. An unprivileged tx-pool submitter can fill the pool with many small transactions to maximize iteration cost, stalling the write lock and degrading block processing throughput.

---

### Finding Description

`_update_tx_pool_for_reorg` in `tx-pool/src/process.rs` is invoked on every new block. It holds the tx-pool write lock for its entire duration and performs three unbounded iterations over pool contents:

**Loop 1 & 2 — all Gap and all Pending entries:** [1](#0-0) 

```rust
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) { … }
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) { … }
```

Neither loop has a count limit. They scan every entry in the respective status bucket.

**Loop 3 — `remove_expired` scans all pool entries:** [2](#0-1) 

```rust
let removed: Vec<_> = self
    .pool_map
    .iter()
    .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
    .map(|entry| entry.inner.clone())
    .collect();
```

`remove_expired` is called unconditionally at the end of `_update_tx_pool_for_reorg`: [3](#0-2) 

The pool is bounded by `max_tx_pool_size` (bytes), but there is **no transaction count limit**. A minimal CKB transaction is on the order of 100–200 bytes. With the default 180 MB pool, the pool can hold on the order of 900 K–1.8 M entries. All three loops run at O(N) per block, and each `remove_entry` call inside `remove_expired` further triggers `update_ancestors_index_key`, which calls `calc_ancestors` (a BFS over the ancestor graph) for every descendant: [4](#0-3) 

The pool's ancestor-tracking structure has no count limit on descendants: [5](#0-4) 

---

### Impact Explanation

All tx-pool operations (submit transaction, fetch tx, get block template) acquire the same write lock. While `_update_tx_pool_for_reorg` holds it and iterates over a maximally-filled pool, every other tx-pool caller blocks. This can:

- Delay block template generation for miners, causing missed block opportunities.
- Delay transaction relay and submission acknowledgement.
- Cause the node's tx-pool service to fall behind the chain tip under sustained attack, degrading liveness.

The impact is analogous to the reported Solidity DoS: a critical path function iterates an unbounded collection, and the collection can be grown by an unprivileged actor.

---

### Likelihood Explanation

An unprivileged tx-pool submitter (RPC caller or P2P peer) can submit many small, independent transactions each paying the minimum fee rate. Because the pool evicts by fee rate (lowest fee rate first), the attacker only needs to pay slightly above the minimum fee rate to keep entries alive. Filling a 180 MB pool with ~1 M minimum-size transactions is economically feasible on a low-fee network or testnet. The attack is repeatable on every block.

---

### Recommendation

1. **Add a maximum transaction count limit** to `PoolMap` (e.g., `max_tx_pool_count`) enforced at insertion time in `add_entry`, analogous to how `max_ancestors_count` is enforced. [6](#0-5) 

2. **Bound `remove_expired` iterations** by processing at most N entries per call and deferring the rest, or by maintaining a time-ordered index so only actually-expired entries are visited.

3. **Bound the Gap/Pending scan in `_update_tx_pool_for_reorg`** by maintaining separate proposal-indexed sets so the loop does not need to scan all entries.

---

### Proof of Concept

1. Connect to a CKB node via RPC.
2. Submit ~1 M minimal transactions (each spending a distinct UTXO, paying minimum fee rate) until `get_tx_pool_info` reports the pool near `max_tx_pool_size`.
3. Observe that on each new block, `_update_tx_pool_for_reorg` holds the tx-pool write lock for an extended period (measurable via RPC latency on `send_transaction` or `get_block_template` calls issued concurrently).
4. Block template generation latency increases proportionally to pool entry count, degrading miner throughput and node liveness.

The root cause is confirmed at: [7](#0-6) [2](#0-1) [8](#0-7)

### Citations

**File:** tx-pool/src/process.rs (L1039-1113)
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
```

**File:** tx-pool/src/pool.rs (L271-287)
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
```

**File:** tx-pool/src/component/pool_map.rs (L77-84)
```rust
impl PoolMap {
    pub fn new(max_ancestors_count: usize) -> Self {
        PoolMap {
            entries: MultiIndexPoolEntryMap::default(),
            edges: Edges::default(),
            links: TxLinksMap::new(),
            max_ancestors_count,
            total_tx_size: 0,
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
