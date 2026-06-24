All six code claims have been verified against the actual source. Every step of the exploit chain is confirmed:

- `request_limit: None` default [1](#0-0) 
- `unwrap_or(usize::MAX)` resolution [2](#0-1) 
- Dead guard (`u32` vs `usize::MAX`) [3](#0-2) 
- `query_builder.limit(limit)` passes caller value to SQL [4](#0-3) 
- `fetch_all` full heap materialization [5](#0-4) 
- Protective config commented out in template [6](#0-5) 

---

Audit Report

## Title
Unbounded heap allocation via `get_cells`/`get_transactions` RPC when `request_limit` is unset (default `usize::MAX`) — (`util/rich-indexer/src/service.rs`)

## Summary
When `request_limit` is absent from `IndexerConfig` (the production default), `RichIndexerService::new` sets the internal limit to `usize::MAX`. The guard in `get_cells` and `get_transactions` compares a caller-supplied `u32` value against `usize::MAX`; on any 64-bit host this comparison is always false, so the guard never fires. A single RPC call with `limit = 0xFFFFFFFF` causes `SQLXPool::fetch_all` to materialize every matching row — including binary `output_data` blobs — into a heap `Vec`, exhausting process memory and crashing the node.

## Finding Description
`IndexerConfig.request_limit` is `Option<usize>` with a serde default of `None`.

`RichIndexerService::new` resolves `None` to `usize::MAX`:
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
```

`get_cells` (and `get_transactions`) check:
```rust
if limit as usize > self.request_limit { ... }
```
`limit` is `u32` (from `Uint32::value()`). On any 64-bit system: `u32::MAX as usize` = 4,294,967,295, while `usize::MAX` = 18,446,744,073,709,551,615. The condition is always `false` — the guard is a dead branch for every possible caller-supplied value.

The SQL query is issued with `LIMIT <caller_value>` (up to 4,294,967,295 rows). `SQLXPool::fetch_all` calls sqlx's `fetch_all`, which collects all rows into a `Vec<AnyRow>` before returning. There is no streaming path and no memory cap. The `ckb.toml` template acknowledges the risk and recommends `request_limit = 400`, but ships with the line commented out, making the insecure default the production default.

## Impact Explanation
A single unauthenticated JSON-RPC call (`get_cells` or `get_transactions` with `limit=4294967295` and a broad script prefix) against a node with rich-indexer enabled and a populated DB will cause the node process to attempt to allocate all matching rows into memory simultaneously. On mainnet-scale data this exhausts available RAM and triggers an OOM kill, crashing the node. Repeated calls prevent recovery. This matches the allowed High impact: **"Vulnerabilities which could easily crash a CKB node."**

## Likelihood Explanation
Rich-indexer is a documented production feature. The default configuration ships without `request_limit`, and the `ckb.toml` template explicitly leaves the protective setting commented out. Any operator who enables rich-indexer without reading the comment is vulnerable. The RPC endpoint is the standard attack surface; no authentication is required by default. The attacker needs only network access to the RPC port and knowledge of any common script `code_hash` (e.g., the secp256k1 lock, which covers the vast majority of mainnet cells).

## Recommendation
1. Change the default in `RichIndexerService::new` from `usize::MAX` to a safe constant: `config.request_limit.unwrap_or(1000)`.
2. Fix the guard to use a type-consistent comparison, or cap `limit` before it reaches the SQL builder.
3. Consider switching `get_cells` to a streaming/cursor approach (`SQLXPool::fetch` with `try_next`) so that even a large limit does not require full materialization.
4. Uncomment and enforce `request_limit = 400` in the default `ckb.toml`.

## Proof of Concept
```bash
# Node running with rich-indexer enabled, no request_limit in config
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "jsonrpc": "2.0",
    "method": "get_cells",
    "params": [
      {"script": {"code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
                  "hash_type": "type", "args": "0x"},
       "script_type": "lock"},
      "asc",
      "0xffffffff",
      null
    ]
  }'
# Guard: 0xffffffff as usize (4294967295) > usize::MAX (18446744073709551615) → false
# SQL emitted: SELECT ... LIMIT 4294967295
# SQLXPool::fetch_all materializes all live cells into memory → OOM kill
```

### Citations

**File:** util/app-config/src/configs/indexer.rs (L65-65)
```rust
            request_limit: None,
```

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L29-34)
```rust
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
