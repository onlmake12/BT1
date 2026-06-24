Audit Report

## Title
Unbounded JSON-RPC Batch Request Causes RPC Server Resource Exhaustion — (File: rpc/src/server.rs)

## Summary
The CKB JSON-RPC server's batch-size guard (`JSONRPC_BATCH_LIMIT`) is backed by a `OnceLock<usize>` that is only populated when the operator explicitly sets `rpc_batch_limit` in `ckb.toml`. The default shipped config leaves the field commented out, so the `OnceLock` is never written and the `if let Some(batch_size)` guard in `handle_jsonrpc` is never entered. Any RPC caller can POST a single HTTP request containing an arbitrarily large JSON-RPC batch array and force the server to process every call sequentially with no cap.

## Finding Description
`rpc_batch_limit` is declared as `Option<usize>` with no `#[serde(default)]` override needed — serde already defaults absent `Option` fields to `None`. [1](#0-0) 

In `RpcServer::new`, `JSONRPC_BATCH_LIMIT` is only initialized inside an `if let Some(...)` arm. When the config field is `None` (the default), the `OnceLock` is never written. [2](#0-1) 

In `handle_jsonrpc`, the batch-size guard is gated on `JSONRPC_BATCH_LIMIT.get()` returning `Some`. When the `OnceLock` is unset it returns `None`, the guard is skipped, and the entire batch is dispatched unconditionally via `stream::iter(...).then(...)`. [3](#0-2) 

The default `ckb.toml` ships with `rpc_batch_limit` commented out and explicitly documents that there is no limit by default. [4](#0-3) 

The only existing constraint is `max_request_body_size` (10–20 MiB depending on deployment). A minimal valid JSON-RPC call is ~40 bytes, allowing ~250,000–500,000 calls per batch within that limit. Each call is processed sequentially with no concurrency cap, no per-batch memory ceiling, and only a global 30-second `TimeoutLayer` that does not prevent resource exhaustion within the window. [5](#0-4) 

## Impact Explanation
A single unauthenticated HTTP POST saturates the Tokio async runtime processing hundreds of thousands of sequential RPC calls, exhausts heap memory building the response stream, and renders the RPC server unresponsive for the duration of the batch. With expensive calls (`get_block` verbosity=2, `get_transaction`, `get_block_economic_state`), memory pressure can trigger an OOM kill of the entire node process. This matches **"Any local RPC API crash" (Note, 0–500 points)** at minimum, and **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)** when memory exhaustion terminates the process.

## Likelihood Explanation
No authentication, no special privilege, and no prior knowledge beyond the public JSON-RPC API is required. The default `ckb.toml` ships without `rpc_batch_limit` set, so every operator using the generated config without explicitly adding the option is affected. While the RPC port is localhost-bound by default, production deployments (exchanges, dApp backends, block explorers) routinely expose it to internal networks or proxies, and the scope explicitly includes "RPC caller" as a valid attacker role. The attack is trivially repeatable with a single HTTP client.

## Recommendation
Set a safe non-`None` default for `rpc_batch_limit` in `Config` (e.g., `2000`, matching the commented-out example) so the guard is active out of the box without operator action: [1](#0-0) 

The `OnceLock` initialization in `RpcServer::new` should then always be populated regardless of whether the operator overrides the value. [2](#0-1) 

## Proof of Concept
```python
import json, requests

ENDPOINT = "http://127.0.0.1:8114"

# ~250,000 cheap calls fit within a 10 MiB body limit
batch = [{"jsonrpc": "2.0", "method": "ping", "id": i} for i in range(250_000)]

# Single POST; server processes all calls sequentially with no limit
resp = requests.post(ENDPOINT, json=batch, timeout=120)
# RPC server is now saturated; concurrent legitimate requests time out
```
For higher impact, replace `"ping"` with `"get_block"` (verbosity=2) to amplify per-call memory and CPU cost, reducing the batch size needed to exhaust resources or trigger OOM.

### Citations

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
```

**File:** rpc/src/server.rs (L274-296)
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

                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });

                (
                    [(axum::http::header::CONTENT_TYPE, "application/json")],
                    StreamBodyAs::json_array(stream),
                )
                    .into_response()
            }
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
