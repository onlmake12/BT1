### Title
Unbounded JSON-RPC Batch Processing with No Default Limit — (`rpc/src/server.rs`)

### Summary

The `handle_jsonrpc` function in `rpc/src/server.rs` processes JSON-RPC batch requests without enforcing any limit when `rpc_batch_limit` is not configured. The default `ckb.toml` explicitly leaves `rpc_batch_limit` commented out, meaning `JSONRPC_BATCH_LIMIT` is `None` at runtime and the guard is skipped entirely. Compounding this, the `max_request_body_size` field (10 MiB default) is present in the config struct but is **never applied as axum middleware** in the server setup, leaving the HTTP body size effectively unbounded.

### Finding Description

**Root cause 1 — batch limit guard is skipped by default:** [1](#0-0) 

`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is only initialized when `config.rpc_batch_limit` is `Some(...)`: [2](#0-1) 

In the batch path, the guard is: [3](#0-2) 

When `JSONRPC_BATCH_LIMIT.get()` returns `None` (the default), the entire `if let Some(...)` block is skipped and all calls are processed unconditionally: [4](#0-3) 

The default `ckb.toml` explicitly documents this gap: [5](#0-4) 

**Root cause 2 — `max_request_body_size` is configured but never enforced:**

The config field exists: [6](#0-5) 

The default value is 10 MiB: [7](#0-6) 

However, the axum router setup applies no `RequestBodyLimitLayer` or equivalent middleware: [8](#0-7) 

The `max_request_body_size` value is read from config but never passed to the router. The HTTP body is read as raw `Bytes` with no enforced size cap, then parsed in full via `serde_json::from_str::<Request>(req)` before any batch limit check is reached.

**Partial mitigations that exist:**

- A 30-second `TimeoutLayer` bounds CPU time per request (lines 125–128), but memory is allocated during body read and JSON parsing *before* the timeout can interrupt processing.
- The default RPC listen address is `127.0.0.1:8114` (localhost only), which limits the attacker surface to local processes or nodes that have explicitly exposed their RPC publicly.

### Impact Explanation

An attacker who can reach the RPC endpoint (locally, or on nodes with publicly exposed RPC — common for mining pools and dApp backends) can:

1. Send a single HTTP POST with a JSON array of thousands of minimal calls (e.g., `{"jsonrpc":"2.0","method":"ping","id":N}`).
2. The body is read entirely into memory with no size cap enforced.
3. `serde_json` allocates a `Vec<Call>` for all entries.
4. With `JSONRPC_BATCH_LIMIT` being `None`, all calls are dispatched sequentially via `stream::iter(calls).then(...)`.
5. Memory and CPU are consumed proportionally to batch size, potentially exhausting node resources within the 30-second window.

The "crash the entire CKB network" claim is overstated — the impact is scoped to individual nodes with exposed RPC endpoints, not the consensus layer. However, a degraded or crashed node cannot relay transactions or blocks, which is a meaningful DoS impact.

### Likelihood Explanation

- The default config leaves `rpc_batch_limit` commented out, so every node running with default settings is affected.
- The `max_request_body_size` non-enforcement is a code-level bug (not a config choice), making it present regardless of operator intent.
- Nodes that expose RPC publicly (mining pools, exchanges, dApp providers) are directly reachable by external attackers.
- The exploit requires only a single HTTP POST — no authentication, no PoW, no privileged access.

### Recommendation

1. **Enforce `max_request_body_size`** by adding a `RequestBodyLimitLayer` to the axum router using the configured value.
2. **Set a safe default for `rpc_batch_limit`** (e.g., 2000, as suggested in the commented-out config) so nodes without explicit configuration are protected.
3. Consider rejecting batch requests entirely before parsing when the `Content-Length` header exceeds the body size limit.

### Proof of Concept

```bash
# Generate a batch of 50,000 minimal ping calls
python3 -c "
import json, sys
calls = [{'jsonrpc':'2.0','method':'ping','id':i} for i in range(50000)]
sys.stdout.write(json.dumps(calls))
" > batch.json

# Send to a node with default config (no rpc_batch_limit set)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data-binary @batch.json &

# Monitor memory growth
watch -n1 'ps aux | grep ckb | grep -v grep'
```

Expected: node memory spikes proportionally to batch size; with sufficiently large batches, the node becomes unresponsive to legitimate RPC calls within the 30-second window.

### Citations

**File:** rpc/src/server.rs (L34-34)
```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
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

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```
