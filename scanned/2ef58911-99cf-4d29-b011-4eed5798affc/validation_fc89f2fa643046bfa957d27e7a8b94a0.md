### Title
O(n) Linear Scan in `get_max_update_time` Over Unboundedly-Fillable Pool Entries Enables RPC DoS - (File: `tx-pool/src/component/pool_map.rs`)

### Summary

`PoolMap::get_max_update_time()` performs a full O(n) linear scan over every entry in the tx-pool to compute the maximum `timestamp` value. The pool already maintains several aggregate statistics (`total_tx_size`, `total_tx_cycles`, `pending_count`, `gap_count`, `proposed_count`) as incrementally-updated cached fields, but `last_txs_updated_at` is not — it is recomputed from scratch on every call. This function is invoked on every `tx_pool_info` RPC call, which is publicly accessible. An unprivileged user can fill the pool with many small transactions and then repeatedly call `tx_pool_info`, causing each call to perform an expensive full-pool scan.

---

### Finding Description

In `tx-pool/src/component/pool_map.rs`, the function `get_max_update_time` iterates over the entire `entries` collection:

```rust
pub(crate) fn get_max_update_time(&self) -> u64 {
    self.entries
        .iter()
        .map(|(_, entry)| entry.inner.timestamp)
        .max()
        .unwrap_or(0)
}
``` [1](#0-0) 

This is called from `tx-pool/src/service.rs` to populate the `last_txs_updated_at` field in the `TxPoolInfo` response for the `tx_pool_info` RPC. [2](#0-1) 

By contrast, the pool already maintains `total_tx_size`, `total_tx_cycles`, `pending_count`, `gap_count`, and `proposed_count` as O(1) cached fields updated incrementally on every add/remove: [3](#0-2) 

The `last_txs_updated_at` field in `TxPoolInfo` is the only aggregate that is not cached: [4](#0-3) 

---

### Impact Explanation

Every call to the `tx_pool_info` RPC endpoint acquires a read lock on the tx-pool and calls `get_max_update_time`, which scans all pool entries. With the default `max_tx_pool_size` of ~180 MB and minimum transaction sizes of ~100–200 bytes, the pool can hold hundreds of thousands of entries. An attacker who fills the pool and then hammers `tx_pool_info` forces repeated O(n) scans, consuming significant CPU on the node's async runtime. This degrades RPC responsiveness and can starve other pool operations that share the same lock. [1](#0-0) 

---

### Likelihood Explanation

The `tx_pool_info` RPC is publicly accessible with no built-in rate limiting in the codebase. Any RPC caller can invoke it repeatedly. Filling the pool requires paying transaction fees, but the cost is bounded and the attack can be sustained by recycling UTXOs. The combination of a large pool and a high-frequency RPC caller is a realistic scenario on a public node. [5](#0-4) 

---

### Recommendation

Maintain `last_txs_updated_at` as a cached field in `PoolMap`, updated incrementally:

- On **add**: set `self.last_txs_updated_at = max(self.last_txs_updated_at, entry.timestamp)`.
- On **remove**: if the removed entry held the maximum timestamp, recompute once (or use a monotonic wall-clock update instead of tracking per-entry timestamps).

This mirrors the existing pattern for `total_tx_size` and `total_tx_cycles`: [6](#0-5) 

Alternatively, `last_txs_updated_at` can be redefined as the wall-clock time of the last pool mutation (add or remove), which is trivially O(1) to maintain and semantically equivalent for its intended use.

---

### Proof of Concept

1. Submit enough small transactions to fill the tx-pool to `max_tx_pool_size` (e.g., ~900,000 minimal transactions at ~200 bytes each).
2. In a tight loop, call `tx_pool_info` via JSON-RPC:
   ```bash
   while true; do
     curl -s -X POST http://localhost:8114 \
       -H 'Content-Type: application/json' \
       -d '{"id":1,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
   done
   ```
3. Observe that each call triggers a full O(n) scan over all pool entries inside `get_max_update_time`, causing sustained CPU load proportional to pool size and call frequency. [1](#0-0) [7](#0-6)

### Citations

**File:** tx-pool/src/component/pool_map.rs (L60-90)
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

**File:** tx-pool/src/component/pool_map.rs (L130-136)
```rust
    pub(crate) fn get_max_update_time(&self) -> u64 {
        self.entries
            .iter()
            .map(|(_, entry)| entry.inner.timestamp)
            .max()
            .unwrap_or(0)
    }
```

**File:** tx-pool/src/service.rs (L370-388)
```rust
    /// Clears the tx-pool, removing all txs, update snapshot.
    pub fn clear_pool(&self, new_snapshot: Arc<Snapshot>) -> Result<(), AnyError> {
        send_message!(self, ClearPool, new_snapshot)
    }

    /// Clears the tx-verify-queue.
    pub fn clear_verify_queue(&self) -> Result<(), AnyError> {
        send_message!(self, ClearVerifyQueue, ())
    }

    /// Returns information about all transactions in the pool.
    pub fn get_all_entry_info(&self) -> Result<TxPoolEntryInfo, AnyError> {
        send_message!(self, GetAllEntryInfo, ())
    }

    /// Returns the IDs of all transactions in the pool.
    pub fn get_all_ids(&self) -> Result<TxPoolIds, AnyError> {
        send_message!(self, GetAllIds, ())
    }
```

**File:** util/types/src/core/tx_pool.rs (L350-352)
```rust
    /// Last updated time. This is the Unix timestamp in milliseconds.
    pub last_txs_updated_at: u64,
    /// Limiting transactions to tx_size_limit
```

**File:** util/jsonrpc-types/src/pool.rs (L12-64)
```rust
/// Transaction pool information.
#[derive(Clone, Default, Serialize, Deserialize, PartialEq, Eq, Hash, Debug, JsonSchema)]
pub struct TxPoolInfo {
    /// The associated chain tip block hash.
    ///
    /// The transaction pool is stateful. It manages the transactions which are valid to be
    /// committed after this block.
    pub tip_hash: H256,
    /// The block number of the block `tip_hash`.
    pub tip_number: BlockNumber,
    /// Count of transactions in the pending state.
    ///
    /// The pending transactions must be proposed in a new block first.
    pub pending: Uint64,
    /// Count of transactions in the proposed state.
    ///
    /// The proposed transactions are ready to be committed in the new block after the block
    /// `tip_hash`.
    pub proposed: Uint64,
    /// Count of orphan transactions.
    ///
    /// An orphan transaction has an input cell from the transaction which is neither in the chain
    /// nor in the transaction pool.
    pub orphan: Uint64,
    /// Total size of transactions bytes in the pool of all the different kinds of states (excluding orphan transactions).
    pub total_tx_size: Uint64,
    /// Total consumed VM cycles of all the transactions in the pool (excluding orphan transactions).
    pub total_tx_cycles: Uint64,
    /// Fee rate threshold. The pool rejects transactions which fee rate is below this threshold.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_fee_rate: Uint64,
    /// RBF rate threshold.
    ///
    /// The pool rejects to replace transactions whose fee rate is below this threshold.
    /// if min_rbf_rate > min_fee_rate then RBF is enabled on the node.
    ///
    /// The unit is Shannons per 1000 bytes transaction serialization size in the block.
    pub min_rbf_rate: Uint64,
    /// Last updated time. This is the Unix timestamp in milliseconds.
    pub last_txs_updated_at: Timestamp,
    /// Limiting transactions to tx_size_limit
    ///
    /// Transactions with a large size close to the block size limit may not be packaged,
    /// because the block header and cellbase are occupied,
    /// so the tx-pool is limited to accepting transaction up to tx_size_limit.
    pub tx_size_limit: Uint64,
    /// Total limit on the size of transactions in the tx-pool
    pub max_tx_pool_size: Uint64,

    /// verify_queue size
    pub verify_queue_size: Uint64,
}
```
