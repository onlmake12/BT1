### Title
Unbounded Rich Indexer RPC Query Causes Memory Exhaustion and Node Unresponsiveness — (`File: util/rich-indexer/src/service.rs`)

---

### Summary

The Rich Indexer RPC endpoints (`get_cells`, `get_transactions`, `get_cells_capacity`) have no effective upper bound on query result size when `request_limit` is not explicitly configured. The default value is `usize::MAX`, which means the guard check is permanently bypassed for any `Uint32` limit value. An unprivileged RPC caller can submit a single request with `limit = 0xFFFFFFFF` (4,294,967,295) and force the node to attempt to fetch, allocate, and JSON-serialize billions of database rows, causing memory exhaustion and node unresponsiveness. The codebase itself documents this risk in the configuration comments.

---

### Finding Description

`RichIndexerService::new()` initializes `request_limit` from the optional config field, falling back to `usize::MAX` when the operator has not set it: [1](#0-0) 

The `get_cells` handler enforces the limit with: [2](#0-1) 

On a 64-bit system, `usize::MAX` is `18,446,744,073,709,551,615`. The `limit` parameter is a `Uint32`, whose maximum value is `4,294,967,295`. The comparison `limit as usize > self.request_limit` is therefore **always false** when `request_limit = usize::MAX`, making the guard a no-op. The same pattern exists in `get_transactions`: [3](#0-2) 

The `get_cells_capacity` endpoint has **no limit parameter and no `request_limit` check at all**. It executes an unbounded `SUM(output.capacity)` aggregation over all matching cells: [4](#0-3) 

The configuration file explicitly acknowledges the risk but leaves the safe value as opt-in: [5](#0-4) 

The `IndexerConfig` struct confirms `request_limit` is `Option<usize>` with no default: [6](#0-5) 

---

### Impact Explanation

When `request_limit` is not configured (the default), an RPC caller submitting `limit = 0xFFFFFFFF` causes:

1. The SQL query is issued with `LIMIT 4294967295` against the SQLite or PostgreSQL database.
2. `self.store.fetch_all(query)` attempts to collect all matching rows into a `Vec<IndexerCell>` or `Vec<IndexerTx>` in memory.
3. JSON serialization of the result (via serde) consumes approximately 10× the raw data size per the codebase's own comment.
4. On a node with millions of indexed cells, this can exhaust available RAM and cause the process to be OOM-killed or become unresponsive, taking down all RPC services including consensus-critical ones.

For `get_cells_capacity`, even a moderately broad search key (e.g., `script_search_mode: prefix` with a short `args` prefix) triggers a full-table aggregation scan with no bound, consuming CPU and I/O proportional to the entire indexed dataset.

---

### Likelihood Explanation

The RPC is reachable by any local or network-accessible RPC caller. No authentication is required. The vulnerable default (`request_limit = usize::MAX`) is the out-of-the-box behavior — operators must explicitly opt in to the safe value. A single malformed request is sufficient to trigger the condition. The attack requires no special knowledge of the chain state; a wildcard search key (empty `args` with `prefix` mode) maximizes the result set.

---

### Recommendation

1. **Set a safe hard default** for `request_limit` in `RichIndexerService::new()` instead of `usize::MAX`. The codebase's own comment recommends `400` as a safe value for a 2 GB memory budget:

   ```rust
   // util/rich-indexer/src/service.rs
   request_limit: config.request_limit.unwrap_or(400),
   ```

2. **Add a `request_limit` check to `get_cells_capacity`**. Since it performs an unbounded aggregation, it should either enforce a maximum result set size or add a configurable timeout.

3. **Apply the same fix to the built-in (RocksDB) indexer**, which has the identical pattern: [7](#0-6) 

---

### Proof of Concept

```json
// Step 1: Confirm the default request_limit is usize::MAX (no config set)
// util/rich-indexer/src/service.rs line 51:
//   request_limit: config.request_limit.unwrap_or(usize::MAX)

// Step 2: Send a single RPC call with maximum Uint32 limit
{
  "id": 1,
  "jsonrpc": "2.0",
  "method": "get_cells",
  "params": [
    {
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock",
      "script_search_mode": "prefix"
    },
    "asc",
    "0xffffffff"
  ]
}

// Step 3: The guard check in get_cells.rs line 29:
//   if limit as usize > self.request_limit
//   => 4294967295 > 18446744073709551615  => false  => guard bypassed

// Step 4: SQL issued: SELECT ... FROM output ... LIMIT 4294967295
// Step 5: fetch_all() collects all indexed cells into memory
// Step 6: serde JSON serialization at ~10x raw size exhausts RAM
// Node becomes unresponsive / OOM-killed

// Variant: get_cells_capacity with no limit at all
{
  "id": 2,
  "jsonrpc": "2.0",
  "method": "get_cells_capacity",
  "params": [
    {
      "script": {
        "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
        "hash_type": "type",
        "args": "0x"
      },
      "script_type": "lock",
      "script_search_mode": "prefix"
    }
  ]
}
// No limit parameter exists; full-table SUM aggregation runs unconditionally
```

### Citations

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L25-34)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_transactions.rs (L23-32)
```rust
        let limit = limit.value();
        if limit == 0 {
            return Err(Error::invalid_params("limit should be greater than 0"));
        }
        if limit as usize > self.request_limit {
            return Err(Error::invalid_params(format!(
                "limit must be less than {}",
                self.request_limit,
            )));
        }
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells_capacity.rs (L13-16)
```rust
    pub async fn get_cells_capacity(
        &self,
        search_key: IndexerSearchKey,
    ) -> Result<Option<IndexerCellsCapacity>, Error> {
```

**File:** resource/ckb.toml (L286-291)
```text
# # By default, there is no limitation on the size of indexer request
# # However, because serde json serialization consumes too much memory(10x),
# # it may cause the physical machine to become unresponsive.
# # We recommend a consumption limit of 2g, which is 400 as the limit,
# # which is a safer approach
# request_limit = 400
```

**File:** util/app-config/src/configs/indexer.rs (L38-41)
```rust
    /// limit of indexer request
    #[serde(default)]
    pub request_limit: Option<usize>,
    /// limit of indexer request timeout
```

**File:** util/indexer/src/service.rs (L98-99)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```
