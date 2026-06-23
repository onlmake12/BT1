Now I have all the code I need to make a definitive determination. Let me trace the full path precisely.

The code evidence is conclusive. Here is the full trace:

**Step 1 — Default produces `usize::MAX`**

`IndexerConfig.request_limit` defaults to `None`: [1](#0-0) [2](#0-1) 

`RichIndexerService::new` converts `None` → `usize::MAX`: [3](#0-2) 

**Step 2 — The guard is mathematically impossible to trigger**

`get_cells` checks `limit as usize > self.request_limit`: [4](#0-3) 

`limit` is `u32`. On any 64-bit system: `u32::MAX as usize` = 4,294,967,295, while `usize::MAX` = 18,446,744,073,709,551,615. The condition `u32::MAX as usize > usize::MAX` is always `false`. The guard is a dead branch for every possible caller-supplied value. The identical dead guard exists in `get_transactions`: [5](#0-4) 

**Step 3 — SQL LIMIT is set to the caller-supplied value** [6](#0-5) 

With `limit = u32::MAX`, the emitted SQL is `SELECT … LIMIT 4294967295`.

**Step 4 — `fetch_all` materializes all matching rows into a heap `Vec`** [7](#0-6) 

There is no streaming, no row-count cap, and no memory budget. Every matching `AnyRow` (including the `output_data` blob column) is allocated on the heap before the first byte is returned to the caller.

**Step 5 — The `ckb.toml` template explicitly acknowledges the risk but leaves the protection commented out (opt-in)** [8](#0-7) 

The comment reads: *"By default, there is no limitation on the size of indexer request. However, because serde json serialization consumes too much memory (10x), it may cause the physical machine to become unresponsive. We recommend … 400 as the limit."* The line `request_limit = 400` is commented out, so every operator who does not explicitly add it ships with `usize::MAX`.

---

### Title
Unbounded heap allocation via `get_cells`/`get_transactions` RPC when `request_limit` is unset (default `usize::MAX`) — (`util/rich-indexer/src/service.rs`)

### Summary
When `request_limit` is absent from `IndexerConfig` (the production default), `RichIndexerService::new` sets the internal limit to `usize::MAX`. The guard in `get_cells` and `get_transactions` compares a `u32` caller-supplied limit against `usize::MAX`; on any 64-bit host this comparison is always false, so the guard never fires. A single RPC call with `limit = 0xFFFFFFFF` causes `SQLXPool::fetch_all` to materialize every matching row — including binary `output_data` blobs — into a heap `Vec`, exhausting process memory and crashing the node.

### Finding Description
- `IndexerConfig.request_limit` is `Option<usize>` with a serde default of `None`.
- `RichIndexerService::new` resolves `None` to `usize::MAX` (service.rs:51).
- `get_cells` (and `get_transactions`) check `limit as usize > self.request_limit`. Because `limit` is `u32` and `u32::MAX < usize::MAX`, this is always `false` — the guard is unreachable for any caller-supplied value.
- The SQL query is issued with `LIMIT <caller_value>` (up to 4,294,967,295).
- `SQLXPool::fetch_all` calls sqlx's `fetch_all`, which collects all rows into a `Vec<AnyRow>` before returning. There is no streaming path and no memory cap.
- The `ckb.toml` template acknowledges the risk and recommends `request_limit = 400`, but ships with the line commented out, making the insecure default the production default.

### Impact Explanation
A single unauthenticated JSON-RPC call (`get_cells` or `get_transactions` with `limit=4294967295` and a broad script prefix) against a node with rich-indexer enabled and a populated DB will cause the node process to attempt to allocate all matching rows into memory simultaneously. On mainnet-scale data (hundreds of millions of live cells) this exhausts available RAM and triggers an OOM kill, crashing the node. Repeated calls prevent recovery.

### Likelihood Explanation
Rich-indexer is a documented production feature. The default configuration ships without `request_limit`, and the `ckb.toml` template explicitly leaves the protective setting commented out. Any operator who enables rich-indexer without reading the comment is vulnerable. The RPC endpoint is the standard attack surface; no authentication is required by default.

### Recommendation
1. Change the default in `RichIndexerService::new` from `usize::MAX` to a safe constant (e.g., 1000 or the recommended 400): `config.request_limit.unwrap_or(1000)`.
2. Fix the guard to use a type-consistent comparison, or cap `limit` before it reaches the SQL builder.
3. Consider switching `get_cells` to a streaming/cursor approach (`SQLXPool::fetch` with `try_next`) so that even a large limit does not require full materialization.
4. Uncomment and enforce `request_limit = 400` in the default `ckb.toml`.

### Proof of Concept
```
# Node running with rich-indexer enabled, no request_limit in config
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "jsonrpc": "2.0",
    "method": "get_cells",
    "params": [
      {"script": {"code_hash": "0x0000...0000", "hash_type": "data", "args": "0x"},
       "script_type": "lock"},
      "asc",
      "0xffffffff",
      null
    ]
  }'
# SQLXPool::fetch_all materializes all live cells into memory → OOM
```

### Citations

**File:** util/app-config/src/configs/indexer.rs (L39-40)
```rust
    #[serde(default)]
    pub request_limit: Option<usize>,
```

**File:** util/app-config/src/configs/indexer.rs (L65-65)
```rust
            request_limit: None,
```

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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L156-156)
```rust
        query_builder.limit(limit);
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

**File:** util/rich-indexer/src/store.rs (L143-149)
```rust
    pub async fn fetch_all<'a, T>(&self, query: Query<'a, Any, T>) -> Result<Vec<AnyRow>>
    where
        T: Send + IntoArguments<'a, Any> + 'a,
    {
        let pool = self.get_pool()?;
        query.fetch_all(pool).await.map_err(Into::into)
    }
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
