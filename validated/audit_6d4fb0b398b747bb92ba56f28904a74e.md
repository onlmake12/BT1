### Title
Unbounded Iteration Over All Pool Entries in `get_raw_tx_pool` RPC Causes Sustained CPU/Memory DoS — (`rpc/src/module/pool.rs`)

---

### Summary

The `get_raw_tx_pool` RPC handler unconditionally iterates over every entry in the transaction pool with no pagination, no result limit, and no rate-limiting guard. As the pool grows, each call performs O(N) work proportional to the total number of pooled transactions. An RPC caller who first fills the pool with many small transactions can then repeatedly invoke this endpoint to cause sustained CPU and memory exhaustion on the node, degrading or blocking normal tx-pool operations.

---

### Finding Description

`get_raw_tx_pool` is a public JSON-RPC method exposed by the `PoolRpc` trait. Its implementation in `rpc/src/module/pool.rs` dispatches to one of two pool-wide scan functions depending on the `verbose` flag:

```rust
// rpc/src/module/pool.rs:703-718
fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
    let tx_pool = self.shared.tx_pool_controller();
    let raw = if verbose.unwrap_or(false) {
        let info = tx_pool.get_all_entry_info()...;   // full scan
        RawTxPool::Verbose(info.into())
    } else {
        let ids = tx_pool.get_all_ids()...;           // full scan
        RawTxPool::Ids(ids.into())
    };
    Ok(raw)
}
```

`get_all_entry_info` (verbose path) iterates over every pending, gap, proposed, and conflicted entry in the pool, collecting full metadata for each:

```rust
// tx-pool/src/pool.rs:464-487
pub(crate) fn get_all_entry_info(&self) -> TxPoolEntryInfo {
    let pending = self.pool_map
        .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();
    let proposed = self.pool_map.sorted_proposed_iter()
        .map(|entry| (entry.transaction().hash(), entry.to_info()))
        .collect();
    let conflicted = self.conflicts_cache.iter()
        .map(|(_id, tx)| tx.hash())
        .collect();
    TxPoolEntryInfo { pending, proposed, conflicted }
}
```

`get_ids` (non-verbose path) performs the same full scan, collecting every transaction hash:

```rust
// tx-pool/src/pool.rs:448-462
pub(crate) fn get_ids(&self) -> TxPoolIds {
    let pending = self.pool_map
        .score_sorted_iter_by_statuses(vec![Status::Pending, Status::Gap])
        .map(|entry| entry.transaction().hash())
        .collect();
    let proposed = self.pool_map.sorted_proposed_iter()
        .map(|entry| entry.transaction().hash())
        .collect();
    TxPoolIds { pending, proposed }
}
```

The message handler in the tx-pool service acquires a read lock for the entire duration of the scan:

```rust
// tx-pool/src/service.rs:1000-1013
Message::GetAllEntryInfo(Request { responder, .. }) => {
    let tx_pool = service.tx_pool.read().await;
    let info = tx_pool.get_all_entry_info();
    ...
}
Message::GetAllIds(Request { responder, .. }) => {
    let tx_pool = service.tx_pool.read().await;
    let ids = tx_pool.get_ids();
    ...
}
```

There is no pagination parameter, no maximum result count, no per-caller rate limit, and no timeout on the scan. The response size and CPU cost grow linearly with the number of pooled transactions.

A secondary compounding issue exists in `get_pool_tx_detail_info`: its implementation calls `get_ids()` (a full pool scan) just to compute the rank of a single transaction, then additionally calls `calc_descendants` and `calc_ancestors`:

```rust
// tx-pool/src/pool.rs:682-711
pub(crate) fn get_tx_detail(&self, id: &ProposalShortId) -> Option<PoolTxDetailInfo> {
    if let Some(entry) = self.pool_map.get_by_id(id) {
        let ids = self.get_ids();   // full scan for rank computation
        ...
        let res = PoolTxDetailInfo {
            descendants_count: self.pool_map.calc_descendants(id).len(),
            ancestors_count: self.pool_map.calc_ancestors(id).len(),
            ...
        };
    }
}
```

---

### Impact Explanation

An attacker who can reach the RPC endpoint (default: localhost; operators may expose it publicly) can:

1. Submit many small, minimum-fee transactions to fill the pool up to `max_tx_pool_size`.
2. Repeatedly call `get_raw_tx_pool(verbose=true)` in a tight loop.
3. Each call forces a full O(N) scan of all pool entries under a read lock, serializes the entire result into a large JSON object, and allocates proportional memory for the response.

The sustained read-lock contention delays write operations on the pool (new transaction admission, block assembly, reorg handling). The large per-response allocations create GC pressure. Under a high-frequency call pattern, the node's tx-pool service thread can become saturated, degrading block template generation and transaction relay.

---

### Likelihood Explanation

The RPC endpoint requires no authentication by default. Any process with network access to the RPC port can call it. Filling the pool with small transactions is cheap (minimum fee per transaction). The attack requires no cryptographic material, no privileged role, and no majority hashpower. It is reachable by any RPC caller, including a malicious local process or a remote caller if the operator has exposed the RPC port.

---

### Recommendation

1. **Add pagination** to `get_raw_tx_pool`: accept `offset` and `limit` parameters and return only a bounded slice of entries per call.
2. **Add a per-IP or per-connection rate limit** on the `get_raw_tx_pool` and `get_pool_tx_detail_info` RPC methods.
3. **Fix `get_tx_detail`**: compute the pending rank without a full pool scan (e.g., maintain an ordered index or use a direct position lookup).
4. **Cap response size**: enforce a hard maximum on the number of entries returned in a single call, returning a continuation token for the next page.

---

### Proof of Concept

```python
import requests, json, threading

NODE_RPC = "http://127.0.0.1:8114"

# Step 1: fill pool with N minimum-fee transactions (omitted for brevity)
# Step 2: hammer get_raw_tx_pool(verbose=true) in parallel
def spam():
    while True:
        requests.post(NODE_RPC, json={
            "id": 1, "jsonrpc": "2.0",
            "method": "get_raw_tx_pool",
            "params": [True]
        })

threads = [threading.Thread(target=spam) for _ in range(50)]
for t in threads:
    t.start()
# Node tx-pool service becomes saturated; block template generation stalls.
```

The cost per call scales with pool occupancy. With `max_tx_pool_size` set to its default (180 MB) and minimum-size transactions (~100 bytes each), the pool can hold on the order of tens of thousands of entries, making each verbose scan expensive in both CPU and allocation. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/pool.rs (L682-711)
```rust
    pub(crate) fn get_tx_detail(&self, id: &ProposalShortId) -> Option<PoolTxDetailInfo> {
        if let Some(entry) = self.pool_map.get_by_id(id) {
            let ids = self.get_ids();
            let rank_in_pending = if entry.status == Status::Proposed {
                0
            } else {
                let tx_hash = entry.inner.transaction().hash();
                ids.pending
                    .iter()
                    .enumerate()
                    .find(|(_, hash)| &tx_hash == *hash)
                    .map(|r| r.0)
                    .unwrap_or_default()
                    + 1
            };
            let res = PoolTxDetailInfo {
                timestamp: entry.inner.timestamp,
                entry_status: entry.status.to_string(),
                pending_count: self.pool_map.pending_size(),
                rank_in_pending,
                proposed_count: ids.proposed.len(),
                descendants_count: self.pool_map.calc_descendants(id).len(),
                ancestors_count: self.pool_map.calc_ancestors(id).len(),
                score_sortkey: entry.inner.as_score_key().into(),
            };
            Some(res)
        } else {
            None
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
