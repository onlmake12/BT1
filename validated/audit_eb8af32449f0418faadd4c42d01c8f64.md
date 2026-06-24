All cited code is confirmed in the repository. Let me verify the key facts:

**Claim 1 — `JSONRPC_BATCH_LIMIT` only initialized when `rpc_batch_limit` is `Some(...)`:** Confirmed. [1](#0-0) 

**Claim 2 — Batch guard skipped when `OnceLock` is uninitialized:** Confirmed. `.get()` returns `None` and the entire guard block is bypassed. [2](#0-1) 

**Claim 3 — Default `ckb.toml` leaves `rpc_batch_limit` commented out:** Confirmed. [3](#0-2) 

**Claim 4 — `max_request_body_size` defined in config struct:** Confirmed. [4](#0-3) 

**Claim 5 — Set to 10 MiB in default config:** Confirmed. [5](#0-4) 

**Claim 6 — Axum router applies no `RequestBodyLimitLayer`:** Confirmed. The router layers are only `Extension`, `CorsLayer`, `TimeoutLayer`, and `Extension(stream_config)` — no body size enforcement. [6](#0-5) 

**Claim 7 — Body read as raw `Bytes` with no size check before JSON parsing:** Confirmed. [7](#0-6) 

All seven cited facts are accurate. Both root causes are real code-level bugs. The batch processing is sequential via `.then()`, meaning a large batch holds the handler for the full duration. The `max_request_body_size` value is read from config but never wired into the router.

---

Audit Report

## Title
Unbounded JSON-RPC Batch Processing with Unenforced Body Size Limit Enables Node DoS — (`rpc/src/server.rs`)

## Summary
`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is never initialized under the default configuration (where `rpc_batch_limit` is commented out), causing the batch size guard to be silently skipped for every request. Compounding this, the `max_request_body_size` config field (10 MiB) is never applied to the axum router — no `RequestBodyLimitLayer` or `DefaultBodyLimit` is added — so HTTP bodies are read into memory without any size cap before JSON parsing begins. Together, these allow an unauthenticated attacker to send a single HTTP POST with an arbitrarily large JSON batch, consuming unbounded memory and CPU and crashing or severely degrading the node.

## Finding Description
**Root cause 1 — batch limit guard skipped by default:**

`JSONRPC_BATCH_LIMIT` is declared as `static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new()` and is only initialized inside `if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit`. The default `ckb.toml` leaves `rpc_batch_limit` commented out (`# rpc_batch_limit = 2000`), so the `OnceLock` is never written. At request time, the guard `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get() && calls.len() > *batch_size` evaluates `.get()` as `None` and the entire block is skipped. All calls in `Request::Batch(calls)` are then dispatched unconditionally via `stream::iter(calls).then(...)`, which processes them sequentially — one call must complete before the next begins — holding the handler for the full duration of the batch.

**Root cause 2 — `max_request_body_size` never enforced:**

`pub max_request_body_size: usize` is defined in the `Config` struct and set to `10485760` in `ckb.toml`, but `RpcServer::start_server` builds the axum router with only `CorsLayer`, `TimeoutLayer`, and `Extension` layers. No `tower_http::limit::RequestBodyLimitLayer` or axum `DefaultBodyLimit` is added. The handler receives `req_body: Bytes` — the full body already buffered by axum — and immediately calls `serde_json::from_str::<Request>(req)` with no prior size check. The configured limit is read but never used.

**Why existing mitigations fail:**

A 30-second `TimeoutLayer` exists, but the body is fully buffered and `serde_json` allocates the entire `Vec<Call>` *before* any timeout can interrupt processing. A sufficiently large payload exhausts memory within the timeout window.

## Impact Explanation
An attacker reaching the RPC endpoint can send a single unauthenticated HTTP POST containing a JSON array of tens of thousands of minimal calls. The body is buffered in full with no size cap, `serde_json` allocates a `Vec<Call>` for all entries, and all calls are dispatched sequentially. Memory and CPU consumption scale linearly with batch size, potentially crashing or making the node unresponsive to legitimate RPC calls. This matches **High: Vulnerabilities which could easily crash a CKB node** (10001–15000 points). The default listen address is `127.0.0.1:8114`, but nodes with publicly exposed RPC — mining pools, exchanges, dApp backends — are directly reachable by external attackers without any authentication or proof-of-work.

## Likelihood Explanation
Every node running with default settings has `rpc_batch_limit` unset; the default config comment explicitly states "By default, there is no limitation on the size of batch request size." The `max_request_body_size` non-enforcement is a code-level bug present regardless of operator intent or configuration. The exploit requires only a single unauthenticated HTTP POST with no privilege, PoW, or victim interaction. Nodes that expose RPC publicly — a common production pattern for mining pools, exchanges, and dApp backends — are directly reachable by external attackers. The attack is repeatable and requires no special tooling beyond `curl` or `python3`.

## Recommendation
1. **Enforce `max_request_body_size`** by adding `tower_http::limit::RequestBodyLimitLayer::new(config.max_request_body_size)` to the axum router in `start_server`, using the value already present in `RpcConfig`.
2. **Set a safe default for `rpc_batch_limit`** (e.g., 2000, as suggested in the commented-out config) so nodes without explicit configuration are protected. Consider making `rpc_batch_limit` a non-optional field with a safe default rather than `Option<usize>`.
3. Consider checking the `Content-Length` header against `max_request_body_size` before buffering the body, to reject oversized requests at the earliest possible point.

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

**File:** rpc/src/server.rs (L34-55)
```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();

#[doc(hidden)]
#[derive(Debug)]
pub struct RpcServer {
    pub http_address: SocketAddr,
    pub tcp_address: Option<SocketAddr>,
    pub ws_address: Option<SocketAddr>,
}

impl RpcServer {
    /// Creates an RPC server.
    ///
    /// ## Parameters
    ///
    /// * `config` - RPC config options.
    /// * `io_handler` - RPC methods handler. See [ServiceBuilder](../service_builder/struct.ServiceBuilder.html).
    /// * `handler` - Tokio runtime handle.
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
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
