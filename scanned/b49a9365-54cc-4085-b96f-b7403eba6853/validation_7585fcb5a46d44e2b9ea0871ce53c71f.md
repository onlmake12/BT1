Audit Report

## Title
Unbounded JSON-RPC Batch Request Processing by Default Enables Local RPC Resource Exhaustion — (File: `rpc/src/server.rs`)

## Summary
The CKB HTTP JSON-RPC server initialises `JSONRPC_BATCH_LIMIT` only when `rpc_batch_limit` is explicitly set in the configuration. Because the shipped `ckb.toml` leaves this option commented out, the batch-size guard in `handle_jsonrpc` is never active by default. A local process can submit a single HTTP POST containing thousands of JSON-RPC calls, saturating the Tokio async runtime and rendering the RPC server unresponsive for the duration of the request.

## Finding Description
In `rpc/src/server.rs`, `RpcServer::new` conditionally initialises the static `JSONRPC_BATCH_LIMIT`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) 

Because `rpc_batch_limit` is typed `Option<usize>` with no default value, it is `None` unless the operator explicitly sets it. [2](#0-1) 

The shipped `resource/ckb.toml` leaves the option commented out, explicitly noting the risk but providing no default protection: [3](#0-2) 

The batch handler in `handle_jsonrpc` checks the limit only when the `OnceLock` has been populated:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    { ... }
    // proceeds unconditionally when limit is None
``` [4](#0-3) 

When the limit is absent, the entire batch is dispatched as a sequential async stream via `StreamBodyAs::json_array`, with no upper bound on the number of calls processed per request. [5](#0-4) 

The server applies a 30-second `TimeoutLayer`, meaning each oversized batch occupies Tokio worker threads for up to 30 seconds. [6](#0-5) 

The test harness explicitly sets `rpc_batch_limit: Some(1000)`, confirming the production default of `None` is a deliberate but unprotected choice. [7](#0-6) 

## Impact Explanation
A local process can make the RPC server unresponsive by submitting repeated large batch requests, each occupying the async runtime for up to 30 seconds. Legitimate callers (miners, wallets, monitoring tools) are starved of responses. This matches the allowed bounty impact: **Note (0–500 points) — Any local RPC API crash/unresponsiveness**. The claim of High impact (node crash or network congestion) is not substantiated: the attack is bounded by the body size limit and does not prove a full node crash or network-wide effect; the P2P degradation argument is speculative and unproven.

## Likelihood Explanation
The attack requires only the ability to send HTTP POST requests to `127.0.0.1:8114` (the default RPC listen address). Any process running on the same host qualifies with no authentication required. The default configuration ships with the safeguard disabled, and the config comment explicitly acknowledges the risk. Constructing the exploit requires only a standard HTTP client.

## Recommendation
Initialise `JSONRPC_BATCH_LIMIT` to a safe default (e.g., 200) in `RpcServer::new` when `config.rpc_batch_limit` is `None`, so the guard at line 275 is always active. Alternatively, set `rpc_batch_limit = 200` (or similar) as an uncommented default in `resource/ckb.toml`. The test harness already demonstrates the correct pattern with `rpc_batch_limit: Some(1000)`.

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
With `rpc_batch_limit` unset (the default), each POST is accepted and processed without rejection, occupying the async runtime for the full timeout window. Setting `rpc_batch_limit = 200` in `ckb.toml` causes the server to immediately return an error for any batch exceeding 200 entries.

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

**File:** rpc/src/tests/setup.rs (L184-184)
```rust
        rpc_batch_limit: Some(1000),
```
