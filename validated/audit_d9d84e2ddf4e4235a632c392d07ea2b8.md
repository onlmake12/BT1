Audit Report

## Title
Unbounded O(N) Pool Iteration in `_update_tx_pool_for_reorg` Holds tx-pool Write Lock on Every Block - (File: `tx-pool/src/process.rs`)

## Summary
`_update_tx_pool_for_reorg` is called on every new block and holds the tx-pool write lock for its entire duration. It performs three unbounded O(N) iterations over pool contents: scanning all Gap entries, all Pending entries, and all pool entries via `remove_expired`. Because the pool is bounded only by byte size (`max_tx_pool_size = 180 MB`) with no transaction count limit, an unprivileged attacker can fill the pool with many small independent transactions to maximize iteration cost, stalling the write lock and blocking all concurrent tx-pool operations including block template generation.

## Finding Description

**Loop 1 & 2 — Gap and Pending scans** (`tx-pool/src/process.rs` L1065, L1072):

```rust
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Gap) { … }
for entry in tx_pool.pool_map.entries.get_by_status(&Status::Pending) { … }
```

Both loops are unconditional and have no count limit. They execute on every block when `mine_mode` is true. [1](#0-0) 

**Loop 3 — `remove_expired` scans all pool entries** (`tx-pool/src/pool.rs` L274-279):

```rust
let removed: Vec<_> = self
    .pool_map
    .iter()
    .filter(|&entry| self.expiry + entry.inner.timestamp < now_ms)
    .map(|entry| entry.inner.clone())
    .collect();
```

This iterates every entry in the pool unconditionally to find expired ones, then calls `remove_entry` for each expired entry. [2](#0-1) 

`remove_expired` is called unconditionally at the end of `_update_tx_pool_for_reorg`: [3](#0-2) 

**Each `remove_entry` call triggers `update_ancestors_index_key`** (`tx-pool/src/component/pool_map.rs` L242), which calls `calc_ancestors` — a BFS traversal over the ancestor graph: [4](#0-3) [5](#0-4) 

**No transaction count limit exists.** `PoolMap` tracks only `total_tx_size` (bounded by `max_tx_pool_size`) and `max_ancestors_count` (which limits ancestor chain depth per transaction, not total pool entry count). There is no `max_tx_pool_count` field: [6](#0-5) 

The default `max_tx_pool_size` is 180 MB: [7](#0-6) 

With a minimum CKB transaction size of ~100–200 bytes, the pool can hold on the order of 900K–1.8M independent entries. All three loops run at O(N) per block.

## Impact Explanation

All tx-pool operations — `send_transaction`, `get_block_template`, transaction relay — acquire the same write lock (`tokio::sync::RwLock` on `TxPool`). While `_update_tx_pool_for_reorg` holds it and iterates a maximally-filled pool, every other caller blocks. This directly delays block template generation for miners (causing missed block opportunities) and degrades transaction relay throughput. This matches the **High** impact class: *Vulnerabilities or bad designs which could cause CKB network congestion with few costs.*

## Likelihood Explanation

An unprivileged RPC caller or P2P peer can submit many small, independent transactions each paying slightly above the minimum fee rate (1,000 shannons/KB). Because `limit_size` evicts by lowest fee rate, the attacker only needs to outbid the minimum to keep entries alive. On testnet or a low-fee network, filling a 180 MB pool with ~1M minimum-size transactions is economically feasible. The attack is repeatable on every block since `_update_tx_pool_for_reorg` is triggered by every new block. The `max_ancestors_count = 25` limit does not constrain independent (non-chained) transactions. [8](#0-7) 

## Recommendation

1. **Add a maximum transaction count limit** (`max_tx_pool_count`) to `PoolMap`, enforced at insertion in `add_entry`, analogous to how `max_ancestors_count` is enforced per-chain. This directly caps N for all three loops. [9](#0-8) 

2. **Optimize `remove_expired`** by maintaining a timestamp-ordered index so only actually-expired entries are visited, rather than iterating the entire pool. [10](#0-9) 

3. **Optimize the Gap/Pending scans** in `_update_tx_pool_for_reorg` by maintaining proposal-indexed sets so the loops only visit entries whose short IDs appear in the current proposal window, rather than scanning all entries. [11](#0-10) 

## Proof of Concept

1. Connect to a CKB node (testnet or local devnet) via RPC.
2. Generate a large number of distinct UTXOs (e.g., via coinbase outputs on devnet).
3. Submit ~1M minimal independent transactions (each spending a distinct UTXO, paying 1,001 shannons/KB) until `get_tx_pool_info` reports `total_tx_size` near `max_tx_pool_size` (180 MB).
4. Trigger a new block (on devnet, via `generate_block` RPC).
5. Concurrently issue `get_block_template` or `send_transaction` RPC calls and measure response latency.
6. **Expected result:** RPC latency on `get_block_template` and `send_transaction` increases proportionally to pool entry count during block processing, as `_update_tx_pool_for_reorg` holds the write lock while iterating all N entries across three unbounded loops.

The root cause is confirmed at: [1](#0-0) [2](#0-1) [6](#0-5)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

**File:** resource/ckb.toml (L211-216)
```text
max_tx_pool_size = 180_000_000 # 180mb
min_fee_rate = 1_000 # Here fee_rate are calculated directly using size in units of shannons/KB
# min_rbf_rate > min_fee_rate means RBF is enabled
min_rbf_rate = 1_500 # Here fee_rate are calculated directly using size in units of shannons/KB
max_tx_verify_cycles = 70_000_000
max_ancestors_count = 25
```
