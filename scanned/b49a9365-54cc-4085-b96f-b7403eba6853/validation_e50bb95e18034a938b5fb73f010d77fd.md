Both root causes are confirmed in the code. Let me verify the key facts:

1. `JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` only initialized when `config.rpc_batch_limit` is `Some(...)` [1](#0-0) 

2. The batch guard uses `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()` — when uninitialized, `.get()` returns `None` and the entire block is skipped, processing all calls unconditionally. [2](#0-1) 

3. The default `ckb.toml` explicitly leaves `rpc_batch_limit` commented out. [3](#0-2) 

4. `max_request_body_size` is defined in the config struct [4](#0-3)  and set to 10 MiB in the default config [5](#0-4) , but the axum router setup applies no `RequestBodyLimitLayer` or equivalent middleware. [6](#0-5) 

5. The body is read as raw `Bytes` with no size enforcement before full JSON parsing. [7](#0-6) 

---

Audit Report

## Title
Unbounded JSON-RPC Batch Processing with Unenforced Body Size Limit Enables Node DoS — (`rpc/src/server.rs`)

## Summary
The `handle_jsonrpc` function processes JSON-RPC batch requests without any batch count limit when `rpc_batch_limit` is not configured, which is the default. Compounding this, the `max_request_body_size` config field (10 MiB) is never applied as axum middleware, leaving HTTP body reads effectively unbounded. An attacker who can reach the RPC endpoint can send a single HTTP POST with an arbitrarily large JSON array of calls, consuming unbounded memory and CPU and crashing or severely degrading the node.

## Finding Description
**Root cause 1 — batch limit guard skipped by default:**

`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` initialized only when `config.rpc_batch_limit` is `Some(...)`:
```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```
The default `ckb.toml` leaves `rpc_batch_limit` commented out (`# rpc_batch_limit = 2000`), so `JSONRPC_BATCH_LIMIT.get()` returns `None` at runtime. The batch guard:
```rust
if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
    && calls.len() > *batch_size { ... }
```
is skipped entirely, and all calls in `Request::Batch(calls)` are dispatched unconditionally via `stream::iter(calls).then(...)`.

**Root cause 2 — `max_request_body_size` never enforced:**

The config field `pub max_request_body_size: usize` is defined and defaults to 10485760 (10 MiB) in `ckb.toml`, but the axum router setup adds no `RequestBodyLimitLayer` or equivalent. The handler receives `req_body: Bytes` with no size cap, then calls `serde_json::from_str::<Request>(req)` on the full body before any batch limit check is reached. The `max_request_body_size` value is read from config but never passed to the router.

**Partial mitigations that fail to prevent the attack:**

A 30-second `TimeoutLayer` exists, but memory is allocated during body read and `serde_json` parsing *before* the timeout can interrupt processing. A sufficiently large payload can exhaust memory within the timeout window.

## Impact Explanation
An attacker reaching the RPC endpoint can send a single HTTP POST with a JSON array of tens of thousands of minimal calls. The body is read entirely into memory with no size cap, `serde_json` allocates a `Vec<Call>` for all entries, and all calls are dispatched sequentially. Memory and CPU are consumed proportionally to batch size, potentially crashing or making the node unresponsive to legitimate RPC calls. This matches **High: Vulnerabilities which could easily crash a CKB node**. The default listen address is `127.0.0.1:8114`, but nodes with publicly exposed RPC (mining pools, exchanges, dApp backends) are directly reachable by external attackers, and even localhost-bound nodes are vulnerable to any local process.

## Likelihood Explanation
Every node running with default settings has `rpc_batch_limit` unset — the default config comment explicitly states "By default, there is no limitation on the size of batch request size." The `max_request_body_size` non-enforcement is a code-level bug present regardless of operator intent. The exploit requires only a single unauthenticated HTTP POST with no PoW or privilege. Nodes that expose RPC publicly (a common production pattern) are directly reachable by external attackers.

## Recommendation
1. **Enforce `max_request_body_size`** by adding `tower_http::limit::RequestBodyLimitLayer::new(config.max_request_body_size)` to the axum router using the configured value.
2. **Set a safe default for `rpc_batch_limit`** (e.g., 2000, as suggested in the commented-out config) so nodes without explicit configuration are protected. Consider making `rpc_batch_limit` a non-optional field with a safe default.
3. Consider rejecting batch requests before full JSON parsing when the `Content-Length` header exceeds the body size limit.

## Proof of Concept
```bash
# Generate a batch of 50,000 minimal ping calls (~3.5 MB JSON)
python3 -c "
import json, sys
calls = [{'jsonrpc':'2.0','method':'ping','id':i} for i in range(50000)]
sys.stdout.write(json.dumps(calls))
" > batch.json

# Send to a node with default config (no rpc_batch_limit set)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data-binary @batch.json

# Monitor memory growth
watch -n1 'ps aux | grep ckb | grep -v grep'
```
Expected: node memory spikes proportionally to batch size; with sufficiently large batches, the node becomes unresponsive to legitimate RPC calls within the 30-second window. For body size bypass, send a payload exceeding 10 MiB — it will be accepted and parsed in full since no `RequestBodyLimitLayer` is applied.

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
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

**File:** rpc/src/server.rs (L218-238)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
        Json(jsonrpc_core::Failure {
            jsonrpc: Some(jsonrpc_core::Version::V2),
            id: jsonrpc_core::Id::Null,
            error,
        })
        .into_response()
    };

    let req = match std::str::from_utf8(req_body.as_ref()) {
        Ok(req) => req,
        Err(_) => {
            return make_error_response(jsonrpc_core::Error::parse_error());
        }
    };

    let req = serde_json::from_str::<Request>(req);
```

**File:** rpc/src/server.rs (L275-282)
```rust
                if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
                    && calls.len() > *batch_size
                {
                    return make_error_response(jsonrpc_core::Error::invalid_params(format!(
                        "batch size is too large, expect it less than: {}",
                        batch_size
                    )));
                }
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```
