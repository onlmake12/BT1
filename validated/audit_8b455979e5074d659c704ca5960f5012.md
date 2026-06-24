Audit Report

## Title
Unbounded Default `request_limit` Bypasses Pagination Guard, Enabling RPC-Triggered OOM — (`util/rich-indexer/src/service.rs`, `util/indexer/src/service.rs`)

## Summary
Both the built-in indexer and rich-indexer initialize `request_limit` to `usize::MAX` when the operator has not configured it. On any 64-bit host, the upper-bound guard `limit as usize > self.request_limit` is permanently dead code under this default because `u32::MAX` (4,294,967,295) is always less than `usize::MAX` (18,446,744,073,709,551,615). An unprivileged caller can submit `limit = 0xFFFFFFFF` and force the node to issue a `LIMIT 4294967295` SQL query or iterate up to 4.3 billion RocksDB entries, allocating and serializing a result `Vec` proportional to matching records, which can OOM-kill the node process.

## Finding Description

**Root cause — default initialization:**

`IndexerService::new()` sets `request_limit: config.request_limit.unwrap_or(usize::MAX)`. [1](#0-0) 

`RichIndexerService::new()` sets the same default. [2](#0-1) 

`IndexerConfig::default()` sets `request_limit: None`, so `unwrap_or(usize::MAX)` always fires when the operator has not explicitly configured the field. [3](#0-2) 

**Broken guard — `get_cells` and `get_transactions`:**

In `get_cells.rs`, `limit` is extracted as a `u32` via `limit.value()`, then compared against `self.request_limit` (a `usize`). On a 64-bit host, `0xFFFF_FFFF_u32 as usize` = 4,294,967,295, which is strictly less than `usize::MAX` = 18,446,744,073,709,551,615. The guard branch is unreachable under the default configuration. [4](#0-3) 

The identical pattern exists in `get_transactions.rs`. [5](#0-4) 

**Sink — limit passed directly to the database:**

The raw `u32` value is forwarded to the SQL engine as `LIMIT 4294967295`. [6](#0-5) 

**No timeout in rich-indexer:**

`AsyncRichIndexerHandle` has no timeout field. The classic indexer has a `timeout_limit: Duration` (10-second `TimeoutIterator`) as a partial mitigation, but the rich-indexer has none. [7](#0-6) [8](#0-7) 

## Impact Explanation

A node with a large indexed dataset (mainnet nodes routinely index tens of millions of cells) will attempt to allocate and serialize a `Vec` of up to 4.3 billion records. Even a modest matching set of millions of records can exhaust available RAM and trigger an OOM kill of the node process. This maps to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**. At minimum, repeated concurrent requests block all RPC threads for the duration of the query, rendering the node's RPC layer unresponsive.

## Likelihood Explanation

The `request_limit` option is opt-in and commented out in the shipped config template (`resource/ckb.toml`: `# request_limit = 400`), so the default `usize::MAX` applies to all nodes that have not explicitly set it. The RPC port defaults to `127.0.0.1:8114` but is commonly exposed in infrastructure deployments. No authentication is required. The exploit requires a single malformed JSON-RPC call with `"0xffffffff"` as the limit parameter, which is trivially repeatable and requires no special knowledge of the target node.

## Recommendation

1. Change the fallback in both `IndexerService::new()` and `RichIndexerService::new()` from `usize::MAX` to a safe bounded constant (e.g., `4000`) matching the config comment's recommendation.
2. Alternatively, change `request_limit: Option<usize>` to `request_limit: Option<NonZeroUsize>` with a bounded `Default` so the guard always fires.
3. Add a `timeout_limit` field to `AsyncRichIndexerHandle` analogous to the `timeout_limit: Duration` already present in the classic `IndexerHandle`, so long-running SQL queries are cancelled regardless of the limit value.

## Proof of Concept

Against any default-configured CKB node with indexer enabled on a 64-bit host:

```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1, "jsonrpc": "2.0", "method": "get_cells",
    "params": [
      {"script": {"code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
                  "hash_type": "type", "args": "0x"},
       "script_type": "lock"},
      "asc", "0xffffffff", null
    ]
  }'
```

Verification path:
- `limit.value()` = 4,294,967,295 (u32)
- `request_limit` = `usize::MAX` = 18,446,744,073,709,551,615
- Guard: `4294967295 > 18446744073709551615` → `false` → no rejection
- SQL issued: `SELECT ... LIMIT 4294967295`
- Node allocates result `Vec` proportional to matching records and serializes to JSON
- Repeated concurrent requests with this payload exhaust node memory and crash the process on any mainnet node with a non-trivial indexed dataset

### Citations

**File:** util/indexer/src/service.rs (L98-98)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** util/indexer/src/service.rs (L99-99)
```rust
            timeout_limit: Duration::from_secs(config.timeout_limit.unwrap_or(10)),
```

**File:** util/rich-indexer/src/service.rs (L51-51)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
```

**File:** util/app-config/src/configs/indexer.rs (L65-65)
```rust
            request_limit: None,
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L23-27)
```rust
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
}
```
