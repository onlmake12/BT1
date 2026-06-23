### Title
Unbounded JSON-RPC Batch Request Processing Enables Denial of Service by Default — (`File: rpc/src/server.rs`)

### Summary
The CKB JSON-RPC server has no default limit on the number of calls in a JSON-RPC batch request. The `rpc_batch_limit` configuration option is `None` by default (explicitly commented out in the shipped config), meaning any RPC caller can submit a single HTTP POST containing an arbitrarily large batch of JSON-RPC calls. The server processes every call sequentially with no guard, exhausting CPU and memory.

### Finding Description

The `rpc_batch_limit` field in `RpcConfig` is typed as `Option<usize>` and defaults to `None`. [1](#0-0) 

The shipped production config explicitly leaves it disabled: [2](#0-1) 

In `RpcServer::new`, the `JSONRPC_BATCH_LIMIT` static is only initialized when `rpc_batch_limit` is `Some`: [3](#0-2) 

In the HTTP handler `handle_jsonrpc`, the batch-size guard is wrapped in `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()`. When the limit is not configured, `JSONRPC_BATCH_LIMIT.get()` returns `None` and the entire guard is skipped: [4](#0-3) 

All calls in the batch are then dispatched sequentially with no bound: [5](#0-4) 

A secondary compounding factor: the `max_request_body_size` config value (10 MiB) is never passed into `start_server` and no `DefaultBodyLimit` middleware layer is applied to the axum router, so the body size limit is also not enforced as configured: [6](#0-5) 

Within axum's own default body limit (~2 MiB), a minimal JSON-RPC call (`{"jsonrpc":"2.0","method":"ping","id":1}` ≈ 40 bytes) allows roughly 50,000 calls per request. Each call invokes a full RPC dispatch cycle.

### Impact Explanation

An attacker who can reach the RPC port sends a single HTTP POST containing tens of thousands of JSON-RPC calls. The server processes every call before returning a response, consuming CPU proportional to the batch size and holding the response stream open. Repeated requests from multiple connections can saturate the RPC thread pool and exhaust memory, causing the node to become unresponsive to legitimate RPC clients (miners, wallets, indexers).

### Likelihood Explanation

The RPC defaults to `127.0.0.1:8114`, but many operators expose it to wider networks (e.g., behind a reverse proxy, or misconfigured to `0.0.0.0`). Even on localhost, any local process or local CLI user qualifies as an attacker under the bounty scope. The attack requires only a standard HTTP client and knowledge of the JSON-RPC batch format — no authentication, no special privileges, no cryptographic material.

### Recommendation

- **Short term:** Set a safe non-`None` default for `rpc_batch_limit` in `RpcConfig` (e.g., 100–500 calls) so the guard in `handle_jsonrpc` is always active without requiring operator action.
- **Short term:** Apply `tower_http::limit::RequestBodyLimitLayer` (or axum's `DefaultBodyLimit`) using the configured `max_request_body_size` value inside `start_server`, so the body-size limit is actually enforced.
- **Long term:** Deviations from safe defaults (unlimited batch size, unenforced body limit) should require explicit opt-in, not opt-out.

### Proof of Concept

```python
import json, requests

# Build a batch of 50,000 minimal RPC calls (~2 MB total)
batch = [{"jsonrpc": "2.0", "method": "get_tip_block_number", "id": i}
         for i in range(50_000)]

# Single HTTP POST — no authentication required
r = requests.post(
    "http://127.0.0.1:8114",
    json=batch,
    headers={"Content-Type": "application/json"},
    timeout=300,
)
print(r.status_code, len(r.json()))
```

Sending this repeatedly from multiple threads will saturate the RPC worker pool. With `rpc_batch_limit` absent from `ckb.toml` (the default), the server processes all 50,000 calls per request with no rejection.

### Citations

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
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
