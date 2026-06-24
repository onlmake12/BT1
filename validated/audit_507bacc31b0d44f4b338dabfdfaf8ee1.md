All six code citations verified exactly against the source. The finding is valid.

---

Audit Report

## Title
Unbounded JSON-RPC Batch Request Processing by Default Enables Local RPC Resource Exhaustion — (File: `rpc/src/server.rs`)

## Summary
`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is only populated when `config.rpc_batch_limit` is explicitly set. Because `resource/ckb.toml` ships with `rpc_batch_limit` commented out, the batch-size guard in `handle_jsonrpc` is permanently inactive under the default configuration. Any local process can submit arbitrarily large JSON-RPC batch arrays, causing sustained CPU and memory pressure on the node process.

## Finding Description
`JSONRPC_BATCH_LIMIT` is declared as a static `OnceLock<usize>` and is only initialised inside the conditional block in `RpcServer::new`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) 

When `rpc_batch_limit` is `None` (the default), `JSONRPC_BATCH_LIMIT.get()` always returns `None`, so the guard in `handle_jsonrpc` is never entered:

```rust
if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
    && calls.len() > *batch_size
{
    return make_error_response(...);
}
``` [2](#0-1) 

The batch is then dispatched as a sequential async stream with no upper bound on the number of calls: [3](#0-2) 

The shipped configuration explicitly acknowledges the risk but leaves the safeguard commented out: [4](#0-3) 

`max_request_body_size` exists in `RpcConfig` but no `DefaultBodyLimit` layer is applied in `start_server`, so the effective body cap is axum's built-in default (2 MiB), not 10 MiB. This is a minor inaccuracy in the report but does not invalidate the core finding — a 2 MiB body can still carry thousands of minimal JSON-RPC call objects. [5](#0-4) 

The `TimeoutLayer` of 30 seconds is confirmed: [6](#0-5) 

## Impact Explanation
Sustained CPU and memory pressure on the local node process from a local caller with no authentication required. This maps to **Note (0–500 points): Any local RPC API crash/degradation**. The claim's attempt to elevate severity to High by asserting P2P and consensus degradation is speculative — no evidence is provided that Tokio worker saturation from RPC batch processing measurably delays the P2P message handler in practice. The higher severity is not proven and cannot be awarded.

## Likelihood Explanation
The RPC server listens on `127.0.0.1:8114` by default, restricting the attacker to the local host. Any process running on the same machine — a malicious dependency, a compromised script, or a local user — qualifies. No credentials or privileges are required. The exploit requires only a standard HTTP client and knowledge of any valid RPC method name. The default configuration ships with the safeguard disabled, and the config comment confirms the risk is known.

## Recommendation
Initialise `JSONRPC_BATCH_LIMIT` to a safe default (e.g., 200) in `RpcServer::new` when `config.rpc_batch_limit` is `None`, so the guard at line 275 is always active regardless of configuration. Alternatively, set `rpc_batch_limit = 200` (or similar) as an uncommented default in `resource/ckb.toml`. Additionally, apply the existing `max_request_body_size` config field as a `DefaultBodyLimit` layer in `start_server` to enforce the intended body cap.

## Proof of Concept
```python
import json, requests

batch = [
    {"jsonrpc": "2.0", "method": "get_tip_block_number", "params": [], "id": i}
    for i in range(5000)
]

while True:
    requests.post(
        "http://127.0.0.1:8114",
        data=json.dumps(batch),
        headers={"Content-Type": "application/json"},
        timeout=35,
    )
```
With `rpc_batch_limit` unset (the default), each POST is accepted and all 5000 calls are processed sequentially. Setting `rpc_batch_limit = 200` in `ckb.toml` causes the server to immediately return an error for any batch exceeding 200 entries, confirming the fix.

### Citations

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

**File:** rpc/src/server.rs (L284-295)
```rust
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
