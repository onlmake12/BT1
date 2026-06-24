Audit Report

## Title
Unbounded Default `request_limit` Bypasses Upper-Bound Guard, Enabling Resource Exhaustion via Indexer Pagination — (`util/indexer/src/service.rs`, `util/rich-indexer/src/service.rs`)

## Summary
Both `IndexerService` and `RichIndexerService` initialize `request_limit` to `usize::MAX` when the operator has not set the option. Because the upper-bound guard compares a `u32` caller-supplied value against `usize::MAX`, the guard never fires on 64-bit hosts, allowing any caller to submit `limit = 0xFFFFFFFF` and force the node to attempt fetching up to 4.3 billion records in a single request. The shipped configuration template explicitly acknowledges that this can render the physical machine unresponsive, yet the default remains unlimited.

## Finding Description
`IndexerService::new()` sets:
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
``` [1](#0-0) 

`RichIndexerService::new()` does the same:
```rust
request_limit: config.request_limit.unwrap_or(usize::MAX),
``` [2](#0-1) 

`IndexerConfig::default()` sets `request_limit: None`: [3](#0-2) 

The guard in both `get_cells` and `get_transactions` is:
```rust
let limit = limit.value(); // u32
if limit as usize > self.request_limit { // usize::MAX on default config
    return Err(...);
}
``` [4](#0-3) [5](#0-4) 

On a 64-bit host, `u32::MAX as usize` (4,294,967,295) is always less than `usize::MAX` (18,446,744,073,709,551,615), so the rejection branch is dead code under the default configuration. The unchecked `limit` value is then passed directly into `query_builder.limit(limit)`: [6](#0-5) 

`AsyncRichIndexerHandle` has no timeout field — only `request_limit`: [7](#0-6) 

The shipped config template explicitly documents the risk but leaves the default unlimited: [8](#0-7) 

## Impact Explanation
A caller submitting `limit = 0xFFFFFFFF` causes the rich-indexer to issue `SELECT ... LIMIT 4294967295` against SQLite or PostgreSQL, or causes the classic indexer to iterate up to 4.3 billion RocksDB entries. The result `Vec` and subsequent JSON serialization (documented as 10× memory amplification) can exhaust all available RAM, triggering an OOM kill of the node process. A small number of concurrent such requests is sufficient to crash the node. This matches the **High** impact class: *Vulnerabilities which could easily crash a CKB node*.

## Likelihood Explanation
The `request_limit` option is opt-in and commented out in the shipped `ckb.toml`, so the vast majority of operator deployments run with the unlimited default. The RPC endpoint requires no authentication. While the default bind address is `127.0.0.1:8114`, any local process or any remote client on nodes where operators have exposed the RPC port can trigger this. The attack is trivially repeatable with a single `curl` invocation and requires no special privileges or knowledge beyond the public RPC schema.

## Recommendation
1. Change the fallback in both `IndexerService::new()` and `RichIndexerService::new()` from `usize::MAX` to a safe bounded constant (e.g., `4000`, consistent with the config comment's recommendation).
2. Alternatively, change `request_limit: Option<usize>` to `request_limit: Option<NonZeroUsize>` (the type is already imported in `indexer.rs`) and supply a bounded `NonZeroUsize` default so the guard always fires.
3. Add a `timeout_limit` field to `AsyncRichIndexerHandle` analogous to the `TimeoutIterator` used in the classic indexer, as a defense-in-depth measure for the rich-indexer path.

## Proof of Concept
Against any default-configured CKB node with the indexer enabled:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1, "jsonrpc": "2.0", "method": "get_cells",
    "params": [
      {"script": {"code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
                  "hash_type": "type", "args": "0x"}, "script_type": "lock"},
      "asc", "0xffffffff", null
    ]
  }'
# limit.value()  = 4294967295
# request_limit  = 18446744073709551615 (usize::MAX)
# Guard fires?   4294967295 > 18446744073709551615 → false → no rejection
# SQL issued:    SELECT ... LIMIT 4294967295
# Effect:        node attempts to allocate and serialize up to 4.3B records → OOM
```
Repeating this request concurrently (e.g., 4–8 parallel connections) accelerates memory exhaustion. A unit test can confirm the guard is dead by asserting that `AsyncRichIndexerHandle::new(store, None, usize::MAX).get_cells(..., Uint32::from(u32::MAX), ...)` does not return an `invalid_params` error.

### Citations

**File:** util/indexer/src/service.rs (L98-98)
```rust
            request_limit: config.request_limit.unwrap_or(usize::MAX),
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/mod.rs (L22-27)
```rust
#[derive(Clone)]
pub struct AsyncRichIndexerHandle {
    store: SQLXPool,
    pool: Option<Arc<RwLock<Pool>>>,
    request_limit: usize,
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
