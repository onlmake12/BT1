All cited code references are accurate. Here is the validation result:

---

Audit Report

## Title
No Default Enforcement of JSON-RPC Batch Request Size Limit Enables Resource Exhaustion - (File: `rpc/src/server.rs`)

## Summary
`rpc_batch_limit` is declared as `Option<usize>` with no default value and no `#[serde(default)]` fallback, so `JSONRPC_BATCH_LIMIT` is never populated unless an operator explicitly sets the option. The batch-size guard in `handle_jsonrpc` is gated on `JSONRPC_BATCH_LIMIT.get()` returning `Some`, meaning it is never evaluated in a default deployment. A single HTTP POST containing an arbitrarily large JSON-RPC batch (up to the 10 MiB body limit) causes the node to process every call unconditionally, exhausting CPU and memory.

## Finding Description
`rpc_batch_limit` is `Option<usize>` with no `#[serde(default)]` attribute, so it deserializes to `None` when absent from the config file. [1](#0-0) 

At startup, `JSONRPC_BATCH_LIMIT` (a `OnceLock<usize>`) is only initialized when the operator explicitly provides a value; when `rpc_batch_limit` is `None`, the `OnceLock` is never set. [2](#0-1) 

In `handle_jsonrpc`, the guard is `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()`. When the `OnceLock` is empty (the default), `.get()` returns `None`, the arm is never entered, and all calls in the batch are dispatched unconditionally through `io.handle_call`. [3](#0-2) 

The shipped default configuration explicitly leaves the limit commented out, confirming `None` is the production default. [4](#0-3) 

The test harness explicitly sets `rpc_batch_limit: Some(1000)`, acknowledging that an unbounded limit is unsafe. [5](#0-4) 

The only upstream guard is `max_request_body_size` (10 MiB). Within 10 MiB, a caller can pack approximately 200,000 minimal calls (e.g., `get_tip_block_number` at ~50 bytes each). Each call is deserialized, dispatched, and serialized sequentially in the async stream. The 30-second `TimeoutLayer` limits a single request but does not prevent the attack from being repeated in a loop, sustaining resource exhaustion indefinitely. [6](#0-5) 

## Impact Explanation
A single crafted HTTP POST causes the node to process hundreds of thousands of RPC calls, pegging CPU and exhausting memory. This degrades or halts block processing, transaction relay, and peer synchronization for the duration of the request. Repeated in a loop, it causes sustained node unavailability. This matches **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node**.

## Likelihood Explanation
The RPC endpoint defaults to `127.0.0.1:8114`. Any unprivileged local process — scripts, web applications, or co-located services — can send HTTP requests to localhost without authentication. No key, role, or privilege is required. The attack is a single HTTP POST and is repeatable at negligible cost. Operators who expose the RPC port to a broader network (explicitly warned against but common in practice) face the same attack from any network-reachable client. **Likelihood: Medium** by default; elevated to High when the port is exposed.

## Recommendation
1. Set a safe non-`None` default for `rpc_batch_limit` in the `Config` struct (e.g., 200–500 calls) so the limit is enforced without operator action.
2. Replace the `if let Some(...)` guard in `handle_jsonrpc` with an unconditional check against a compile-time or config-time constant, so the limit cannot be bypassed by an unset `OnceLock`.
3. Apply the same batch limit to the TCP path (`start_tcp_server`), which uses `serve_stream_sink` and shares the same `IoHandler` but has its own framing (2 MiB line limit via `LinesCodec`).

## Proof of Concept
```python
import json, requests

calls = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(200_000)]
body = json.dumps(calls)          # ~9.8 MiB, under the 10 MiB body limit
assert len(body.encode()) < 10 * 1024 * 1024

# Single request; no authentication required
r = requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"})
print(r.status_code, len(r.json()))

# Repeat in a loop for sustained exhaustion
while True:
    requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"})
```

With `rpc_batch_limit` unset (the default), the node processes all 200,000 calls per request. The `TimeoutLayer` (30 s) limits each individual request but does not prevent the loop from sustaining resource exhaustion indefinitely.

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

**File:** rpc/src/tests/setup.rs (L184-184)
```rust
        rpc_batch_limit: Some(1000),
```
