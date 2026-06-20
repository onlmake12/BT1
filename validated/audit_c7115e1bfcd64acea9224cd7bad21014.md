### Title
Unbounded Iteration Over All Tx-Pool Entries in `get_raw_tx_pool` RPC Causes Unbound CPU/Memory Consumption - (`rpc/src/module/pool.rs`)

---

### Summary

The `get_raw_tx_pool` RPC handler unconditionally iterates over every entry in the transaction pool — up to the 180 MB default pool limit — with no pagination, no result-size cap, and no rate limiting. Any RPC caller can trigger this path repeatedly, causing the node to spend unbounded CPU time serializing the entire pool and allocating an arbitrarily large JSON response in memory.

---

### Finding Description

`get_raw_tx_pool` is a public Pool-module RPC method. Its implementation in `rpc/src/module/pool.rs` dispatches to one of two unbounded full-pool scans depending on the `verbose` flag:

- **`verbose=true`** → calls `tx_pool_controller().get_all_entry_info()`, which in `tx-pool/src/pool.rs` iterates over every pending, gap, proposed, and conflicted entry via `score_sorted_iter_by_statuses` and `sorted_proposed_iter`, collecting a `TxPoolEntryInfo` struct that is then converted to a `TxPoolEntries` JSON object containing one key-value pair per transaction.
- **`verbose=false`** → calls `get_all_ids()`, which performs the same full iteration to collect every transaction hash.

Neither path accepts a `limit` or `after` (cursor) parameter. The pool-service message handler acquires the async read lock on the entire pool for the duration of the scan:

```rust
Message::GetAllEntryInfo(Request { responder, .. }) => {
    let tx_pool = service.tx_pool.read().await;
    let info = tx_pool.get_all_entry_info();   // full scan, no bound
    ...
}
```

The default `max_tx_pool_size` is **180 MB**. With minimum-sized transactions (~200–300 bytes each), the pool can hold hundreds of thousands of entries. The verbose JSON response encodes seven hex fields per entry, producing a response that can be many times larger than the raw pool data itself.

---

### Impact Explanation

An RPC caller who can reach the Pool RPC endpoint can:

1. **Memory exhaustion**: Force the node to allocate a multi-hundred-megabyte (or larger) JSON blob in a single synchronous response, potentially triggering OOM conditions.
2. **CPU exhaustion**: Repeated calls cause continuous full-pool iteration and JSON serialization, monopolizing CPU time on the RPC-serving thread.
3. **Read-lock contention**: The async read lock on the tx pool is held for the entire duration of the scan. While a read lock does not block other readers, it does interact with write-lock acquisition (e.g., new transaction admission), potentially delaying pool writes under sustained RPC load.
4. **Response amplification**: The JSON encoding of a 180 MB pool can produce a response significantly larger than 180 MB, amplifying the memory cost on both the node and any client.

---

### Likelihood Explanation

The RPC is enabled by default in the `Pool` module and is reachable by any process that can connect to the RPC listener. While the default `listen_address` is `127.0.0.1:8114` (localhost), many operators expose the RPC to broader networks. No authentication is required. The call requires no special privilege — it is a read-only query. An attacker who can also submit transactions (via `send_transaction`) can first fill the pool to its 180 MB limit and then hammer `get_raw_tx_pool(verbose=true)` to maximize impact.

---

### Recommendation

1. **Add pagination** to `get_raw_tx_pool`: introduce `limit` (max entries per call) and `after` (cursor) parameters, mirroring the pattern already used by the Indexer RPC (`get_cells`, `get_transactions`).
2. **Cap the maximum response size** server-side, returning an error or truncated result when the pool exceeds a configurable threshold.
3. **Rate-limit** the `get_raw_tx_pool` endpoint at the RPC layer.
4. **Document** the scaling behavior so operators are aware of the risk when exposing the RPC publicly.

---

### Proof of Concept

**Step 1 – Fill the pool to its size limit** (attacker submits many small transactions):
```bash
# Submit transactions until pool reaches max_tx_pool_size (180 MB default)
for i in $(seq 1 N); do
  ckb-cli rpc send_transaction --tx-file small_tx_$i.json
done
```

**Step 2 – Trigger unbounded iteration via RPC**:
```bash
# verbose=true: iterates all entries, serializes 7 fields per tx into one JSON blob
curl -s -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[true],"id":1}'
```

With a full 180 MB pool, this single call forces the node to:
- Acquire the pool read lock
- Iterate over every entry via `score_sorted_iter_by_statuses` and `sorted_proposed_iter`
- Allocate a `HashMap<Byte32, TxEntryInfo>` for all pending/proposed entries
- Serialize the entire result to JSON in memory before sending

Repeating this call in a tight loop sustains high CPU and memory pressure on the node.

---

**Root cause references:**

`get_raw_tx_pool` handler — no limit, no pagination: [1](#0-0) 

`get_all_entry_info` — full unbounded scan of all pool entries: [2](#0-1) 

`get_ids` — full unbounded scan for non-verbose path: [3](#0-2) 

Service handler holds read lock for entire scan duration: [4](#0-3) 

Default pool size limit of 180 MB: [5](#0-4) [6](#0-5)

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

**File:** util/app-config/src/legacy/tx_pool.rs (L19-20)
```rust
// Default max_tx_pool_size 180mb
const DEFAULT_MAX_TX_POOL_SIZE: usize = 180_000_000;
```

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
```
