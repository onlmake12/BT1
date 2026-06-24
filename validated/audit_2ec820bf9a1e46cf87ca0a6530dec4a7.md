Audit Report

## Title
Unbounded JSON-RPC Batch Processing and Unenforced Body Size Limit Enable Local RPC DoS — (`File: rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server ships with `rpc_batch_limit` commented out in `resource/ckb.toml`, leaving `JSONRPC_BATCH_LIMIT` uninitialized and the batch-size guard in `handle_jsonrpc` permanently skipped. Separately, `max_request_body_size` is read from config but never passed to `start_server` and no body-limit middleware is applied to the axum router, so the configured 10 MiB limit is silently unenforced. Together, any caller with local access to port 8114 can submit arbitrarily large batch requests that the server processes sequentially with no rejection path, rendering the RPC server unresponsive.

## Finding Description

**Root cause 1 — No default batch limit:**

`rpc_batch_limit` is typed `Option<usize>` with no non-`None` default: [1](#0-0) 

The shipped `resource/ckb.toml` explicitly leaves it commented out, documenting that there is no limitation by default: [2](#0-1) 

In `RpcServer::new`, `JSONRPC_BATCH_LIMIT` is only initialized when the config value is `Some`, so it remains unset at runtime with the default config: [3](#0-2) 

In `handle_jsonrpc`, the guard is wrapped in `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()`. When the `OnceLock` was never initialized, `get()` returns `None` and the entire check is skipped: [4](#0-3) 

All calls in the batch are then dispatched sequentially with no bound: [5](#0-4) 

**Root cause 2 — Unenforced body size limit:**

`max_request_body_size` is present in the config struct and set to 10 MiB in `resource/ckb.toml`: [6](#0-5) 

`start_server` accepts no `max_request_body_size` parameter: [7](#0-6) 

The axum router is constructed with `CorsLayer`, `TimeoutLayer`, and `Extension` layers only — no `DefaultBodyLimit` or `RequestBodyLimitLayer` is applied anywhere in the router or codebase (confirmed by grep returning zero matches for these patterns in `rpc/src/server.rs`): [8](#0-7) 

The field is read in test setup (`max_request_body_size: 20_000_000`) but is never wired into any middleware even there: [9](#0-8) 

The configured limit is silently ignored. The server will read and attempt to parse any body size the OS and connection allow.

## Impact Explanation

An attacker with local access to the RPC port (default `127.0.0.1:8114`) can submit a single HTTP POST containing an arbitrarily large JSON-RPC batch. The server processes every call before returning a response, consuming CPU proportional to batch size and holding memory for the full response. Repeated requests saturate the RPC worker pool and render the RPC server unresponsive to legitimate callers. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash.**

## Likelihood Explanation

The default binding is `127.0.0.1:8114`, so the attacker must have local access or the operator must have exposed the port. No authentication, no special privileges, and no cryptographic material are required — only a standard HTTP client and knowledge of the JSON-RPC batch format. The attack is trivially repeatable and requires no victim interaction. The vulnerable configuration is the default shipped configuration.

## Recommendation

- Set a safe non-`None` default for `rpc_batch_limit` in `RpcConfig` (e.g., 100–500) so the guard in `handle_jsonrpc` is always active without operator action.
- Pass `max_request_body_size` into `start_server` and apply `axum::extract::DefaultBodyLimit::max(config.max_request_body_size)` as a layer on the router so the configured body-size limit is actually enforced.
- Treat unlimited batch size and unenforced body limit as opt-in deviations, not opt-out.

## Proof of Concept

```python
import json, requests

batch = [{"jsonrpc": "2.0", "method": "get_tip_block_number", "id": i}
         for i in range(50_000)]

r = requests.post(
    "http://127.0.0.1:8114",
    json=batch,
    headers={"Content-Type": "application/json"},
    timeout=300,
)
print(r.status_code, len(r.json()))
```

With `rpc_batch_limit` absent from `ckb.toml` (the default), the server processes all 50,000 calls per request. Sending this repeatedly from multiple local connections will saturate the RPC worker pool and make the RPC server unresponsive to legitimate clients. The absence of a body size limit means the batch payload itself can also be made arbitrarily large (e.g., padding each call's params), compounding memory pressure before any parsing-level rejection occurs.

### Citations

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
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

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L97-102)
```rust
    fn start_server(
        rpc: &Arc<MetaIoHandler<Option<Session>>>,
        address: String,
        handler: Handle,
        enable_websocket: bool,
    ) -> Result<SocketAddr, AnyError> {
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

**File:** rpc/src/server.rs (L274-282)
```rust
            Request::Batch(calls) => {
                if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
                    && calls.len() > *batch_size
                {
                    return make_error_response(jsonrpc_core::Error::invalid_params(format!(
                        "batch size is too large, expect it less than: {}",
                        batch_size
                    )));
                }
```

**File:** rpc/src/server.rs (L284-289)
```rust
                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });
```

**File:** rpc/src/tests/setup.rs (L182-184)
```rust
        max_request_body_size: 20_000_000,
        threads: None,
        rpc_batch_limit: Some(1000),
```
