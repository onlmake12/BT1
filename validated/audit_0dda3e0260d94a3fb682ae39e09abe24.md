### Title
Unbounded Iteration in `get_raw_tx_pool` RPC Causes Node DoS via Memory Exhaustion and Lock Contention - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `get_raw_tx_pool` RPC method iterates over every transaction in the tx pool without any pagination or result-size limit. As the pool grows (bounded only by total byte size, not entry count), a single RPC call can exhaust server memory, hold the tx-pool read lock for an extended period, and render the node unresponsive. Any unprivileged RPC caller can trigger this.

---

### Finding Description

`get_raw_tx_pool` is a publicly-accessible JSON-RPC endpoint that returns all transaction IDs or full verbose entry info for every transaction currently in the pool.

The RPC handler delegates to either `get_all_entry_info()` or `get_ids()` on the `TxPool` struct, both of which unconditionally iterate over the entire pool with no pagination, no limit, and no early termination:

```rust
// rpc/src/module/pool.rs
fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
    let tx_pool = self.shared.tx_pool_controller();
    let raw = if verbose.unwrap_or(false) {
        let info = tx_pool.get_all_entry_info()...;   // iterates ALL entries
        RawTxPool::Verbose(info.into())
    } else {
        let ids = tx_pool.get_all_ids()...;           // iterates ALL entries
        RawTxPool::Ids(ids.into())
    };
    Ok(raw)
}
```

The underlying pool methods:

```rust
// tx-pool/src/pool.rs
pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
    let pending = self.pool_map
        .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();   // no limit
    let proposed = self.pool_map.sorted_proposed_iter()
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();   // no limit
    let conflicted = self.conflicts_cache.iter()
        .map(|(_id, tx)| tx.hash()).collect();
    TxPoolEntryInfo { pending, proposed, conflicted }
}
```

The tx pool is bounded by total serialized byte size (`max_tx_pool_size`, default ~180 MB), not by entry count. With minimum-size transactions (~100–200 bytes each), the pool can hold hundreds of thousands of entries. Calling `get_raw_tx_pool(verbose=true)` on such a pool:

1. Acquires the tx-pool read lock for the entire duration of iteration and serialization.
2. Allocates a `HashMap` containing full `TxEntryInfo` for every entry — potentially gigabytes of heap.
3. Serializes the entire result to JSON before returning.
4. Blocks all concurrent tx-pool write operations (new tx admission, block assembly) for the duration.

The message dispatch path confirms the read lock is held across the full `get_all_entry_info` call:

```rust
// tx-pool/src/service.rs
Message::GetAllEntryInfo(Request { responder, .. }) => {
    let tx_pool = service.tx_pool.read().await;   // lock held
    let info = tx_pool.get_all_entry_info();       // full unbounded scan
    ...
}
```

---

### Impact Explanation

- **Memory exhaustion / OOM**: A pool at capacity with small transactions produces a JSON response orders of magnitude larger than the raw pool data. The node process can be killed by the OS OOM killer.
- **Lock starvation**: The tx-pool read lock is held for the entire scan. Concurrent writers (transaction submission, block assembly via `get_block_template`) are blocked, degrading or halting node operation.
- **RPC thread exhaustion**: Multiple concurrent `get_raw_tx_pool` calls compound the above effects.

The node becomes unable to relay transactions, assemble blocks, or respond to other RPC calls — a complete functional denial of service.

---

### Likelihood Explanation

- `get_raw_tx_pool` is a standard, documented, unauthenticated RPC endpoint callable by any client.
- An attacker can first fill the pool with many small, valid, low-fee transactions (each individually accepted), then repeatedly call `get_raw_tx_pool(verbose=true)`.
- No special privileges, keys, or majority hashpower are required.
- The pool can also reach a large state organically on a busy mainnet node.

---

### Recommendation

Add pagination to `get_raw_tx_pool` (e.g., `limit` + `after` cursor parameters, analogous to the indexer's `get_cells`/`get_transactions` endpoints which already implement this pattern). Enforce a hard cap on the number of entries returned per call. The existing indexer RPC design in `rpc/src/module/indexer.rs` provides a ready template.

---

### Proof of Concept

**Entry path:**
1. Submit `N` minimum-size transactions to fill the pool to `max_tx_pool_size`.
2. Call `get_raw_tx_pool(verbose=true)` via JSON-RPC.

**Root cause trace:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

**No pagination exists anywhere in this call chain.** The `TxPoolEntryInfo` and `TxPoolIds` types are plain unbounded collections: [5](#0-4)

### Citations

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }
```

**File:** tx-pool/src/pool.rs (L448-462)
```rust
    pub(crate) fn get_ids(&self) -> TxPoolIds {
        let pending = self
            .pool_map
            .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
            .map(|entry| entry.transaction().hash())
            .collect();

        let proposed = self
            .pool_map
            .sorted_proposed_iter()
            .map(|entry| entry.transaction().hash())
            .collect();

        TxPoolIds { pending, proposed }
    }
```

**File:** tx-pool/src/pool.rs (L464-487)
```rust
    pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
        let pending = self
            .pool_map
            .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let proposed = self
            .pool_map
            .sorted_proposed_iter()
            .map(|entry| (entry.transaction().hash(), entry.to_info()))
            .collect();

        let conflicted = self
            .conflicts_cache
            .iter()
            .map(|(_id, tx)| tx.hash())
            .collect();
        TxPoolEntryInfo {
            pending,
            proposed,
            conflicted,
        }
    }
```

**File:** tx-pool/src/service.rs (L1000-1013)
```rust
        Message::GetAllEntryInfo(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let info = tx_pool.get_all_entry_info();
            if let Err(e) = responder.send(info) {
                error!("Responder sending get_all_entry_info failed {:?}", e)
            };
        }
        Message::GetAllIds(Request { responder, .. }) => {
            let tx_pool = service.tx_pool.read().await;
            let ids = tx_pool.get_ids();
            if let Err(e) = responder.send(ids) {
                error!("Responder sending get_ids failed {:?}", e)
            };
        }
```

**File:** util/types/src/core/tx_pool.rs (L158-176)
```rust
/// Array of transaction ids
#[derive(Debug, PartialEq, Eq)]
pub struct TxPoolIds {
    /// Pending transaction ids
    pub pending: Vec<Byte32>,
    /// Proposed transaction ids
    pub proposed: Vec<Byte32>,
}

/// All in-pool transaction entry info
#[derive(Debug, PartialEq, Eq)]
pub struct TxPoolEntryInfo {
    /// Pending transaction entry info
    pub pending: HashMap<Byte32, TxEntryInfo>,
    /// Proposed transaction entry info
    pub proposed: HashMap<Byte32, TxEntryInfo>,
    /// Conflicted transaction hash vec
    pub conflicted: Vec<Byte32>,
}
```
