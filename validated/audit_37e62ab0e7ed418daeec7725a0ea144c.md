Audit Report

## Title
Unbounded O(N) Pool Iteration in `_update_tx_pool_for_reorg` Holds tx-pool Write Lock on Every Block - (File: `tx-pool/src/process.rs`)

## Summary
`_update_tx_pool_for_reorg` accepts `&mut TxPool`, requiring the caller to hold the write lock for its entire duration. It performs three unbounded O(N) iterations over pool contents on every new block: scanning all Gap entries, all Pending entries, and all pool entries in `remove_expired`. Because `PoolMap` enforces only a byte-size limit (`max_tx_pool_size = 180 MB`) with no transaction count cap, an attacker can fill the pool with many small independent transactions to maximize iteration cost, stalling the write lock and blocking all concurrent tx-pool operations.

## Finding Description

**Loop 1 & 2 — Gap and Pending scans** execute unconditionally when `mine_mode` is true, iterating every entry in those status buckets:

```rust
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) { … }
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) { … }
``` [1](#0-0) 

**Loop 3 — `remove_expired`** iterates every entry in the pool unconditionally to find expired ones:

```rust
let removed: Vec<_> = self
    .pool_map
    .iter()
    .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
    .map(|entry| entry.inner.clone())
    .collect();
``` [2](#0-1) 

`remove_expired` is called unconditionally at the end of `_update_tx_pool_for_reorg`: [3](#0-2) 

Each `remove_entry` call triggers `update_ancestors_index_key`, which invokes `calc_ancestors` — a BFS traversal over the ancestor graph via `TxLinksMap::calc_relation_ids`: [4](#0-3) [5](#0-4) [6](#0-5) 

Note: for independent (non-chained) transactions, `calc_ancestors` returns an empty set in O(1), so the BFS cost per `remove_entry` is negligible in the independent-transaction attack scenario. The dominant O(N) costs are the full-pool scan in `remove_expired` and the Gap/Pending status scans.

**No transaction count limit exists.** `PoolMap` tracks only `total_tx_size` (bounded by `max_tx_pool_size`) and `max_ancestors_count` (per-chain depth, not total entry count): [7](#0-6) 

The default `max_tx_pool_size` is 180 MB: [8](#0-7) 

## Impact Explanation

All tx-pool operations — `send_transaction`, `get_block_template`, transaction relay — require the write lock on `TxPool`. While `_update_tx_pool_for_reorg` holds it and iterates a maximally-filled pool, every other caller blocks. This directly delays block template generation for miners (causing missed block opportunities) and degrades transaction relay throughput. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

An unprivileged RPC caller or P2P peer can submit many small, independent transactions each paying slightly above the minimum fee rate (1,000 shannons/KB). With a minimum CKB transaction size of ~100–200 bytes, the 180 MB pool can hold on the order of 900K–1.8M independent entries. The `max_ancestors_count = 25` limit does not constrain independent (non-chained) transactions. The attack is repeatable on every block since `_update_tx_pool_for_reorg` is triggered by every new block. On testnet or a low-fee devnet, filling the pool is economically feasible.

## Recommendation

1. **Add a maximum transaction count limit** (`max_tx_pool_count`) to `PoolMap`, enforced at insertion in `add_entry`, analogous to how `max_ancestors_count` is enforced per-chain. This directly caps N for all three loops. [9](#0-8) 

2. **Optimize `remove_expired`** by maintaining a timestamp-ordered index (e.g., a `BTreeMap<timestamp, ProposalShortId>`) so only actually-expired entries are visited, rather than iterating the entire pool. [10](#0-9) 

3. **Optimize the Gap/Pending scans** in `_update_tx_pool_for_reorg` by maintaining proposal-indexed sets so the loops only visit entries whose short IDs appear in the current proposal window, rather than scanning all entries. [11](#0-10) 

## Proof of Concept

1. Connect to a CKB node (testnet or local devnet) via RPC.
2. Generate a large number of distinct UTXOs (e.g., via coinbase outputs on devnet).
3. Submit ~1M minimal independent transactions (each spending a distinct UTXO, paying 1,001 shannons/KB) until `get_tx_pool_info` reports `total_tx_size` near `max_tx_pool_size` (180 MB).
4. Trigger a new block (on devnet, via `generate_block` RPC).
5. Concurrently issue `get_block_template` or `send_transaction` RPC calls and measure response latency.
6. **Expected result:** RPC latency on `get_block_template` and `send_transaction` increases proportionally to pool entry count during block processing, as `_update_tx_pool_for_reorg` holds the write lock while iterating all N entries across three unbounded loops.

### Citations

**File:** tx-pool/src/process.rs (L1061-1107)
```rust
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
```

**File:** tx-pool/src/process.rs (L1109-1113)
```rust
    // Remove expired transaction from pending
    tx_pool.remove_expired(callbacks);

    // Remove transactions from the pool until its size <= size_limit.
    let _ = tx_pool.limit_size(callbacks, None);
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

**File:** tx-pool/src/component/pool_map.rs (L60-75)
```rust
pub struct PoolMap {
    /// The pool entries with different kinds of sort strategies
    pub(crate) entries: MultiIndexPoolEntryMap,
    /// All the deps, header_deps, inputs, outputs relationships
    pub(crate) edges: Edges,
    /// All the parent/children relationships
    pub(crate) links: TxLinksMap,
    pub(crate) max_ancestors_count: usize,
    // sum of all tx_pool tx's virtual sizes.
    pub(crate) total_tx_size: usize,
    // sum of all tx_pool tx's cycles.
    pub(crate) total_tx_cycles: Cycle,
    pub(crate) pending_count: usize,
    pub(crate) gap_count: usize,
    pub(crate) proposed_count: usize,
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

**File:** tx-pool/src/component/pool_map.rs (L242-243)
```rust
            self.update_ancestors_index_key(&entry.inner, EntryOp::Remove);
            self.update_descendants_index_key(&entry.inner, EntryOp::Remove);
```

**File:** tx-pool/src/component/pool_map.rs (L432-434)
```rust
    fn update_ancestors_index_key(&mut self, child: &TxEntry, op: EntryOp) {
        let ancestors: HashSet<ProposalShortId> =
            self.links.calc_ancestors(&child.proposal_short_id());
```

**File:** tx-pool/src/component/links.rs (L52-71)
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
```

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```
