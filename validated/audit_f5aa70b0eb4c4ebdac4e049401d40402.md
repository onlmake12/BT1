The code confirms the vulnerability. `max_request_body_size` is **never referenced anywhere in `rpc/src/server.rs`** — the grep shows zero matches in that file. The value is parsed into `RpcConfig` but silently dropped when `start_server` is called.

---

### Title
`max_request_body_size` Is Configured But Never Enforced — Unbounded HTTP Body Allocation in RPC Server — (`rpc/src/server.rs`)

### Summary
`RpcConfig.max_request_body_size` is a documented, user-configurable field that is supposed to cap incoming HTTP request body size. In practice it is never passed to `start_server` and no axum body-limit middleware is applied, so any caller who can reach the RPC port can stream an arbitrarily large body that axum buffers entirely into heap memory before the handler is invoked.

### Finding Description

`RpcServer::new` receives a full `RpcConfig` but only forwards `config.listen_address` to `start_server`; `config.max_request_body_size` is silently discarded: [1](#0-0) 

`start_server` builds the axum `Router` with four layers — `Extension`, `CorsLayer::permissive()`, `TimeoutLayer`, and a second `Extension` — none of which limit body size: [2](#0-1) 

The POST handler signature is `req_body: Bytes`, which causes axum to collect the complete request body into a contiguous heap allocation before the handler runs: [3](#0-2) 

Only after the full body is in memory does the handler call `std::str::from_utf8` and `serde_json::from_str`: [4](#0-3) 

The config field exists and is documented: [5](#0-4) 

Default value is 10 MiB, but it is never applied: [6](#0-5) 

### Impact Explanation
An attacker who can reach the RPC port sends a single HTTP POST with a multi-hundred-megabyte body. Axum allocates the full body on the heap before any application logic runs. Repeated requests, or one sufficiently large request, can exhaust available RAM, triggering OOM-kill of the node process or inducing severe memory pressure that starves the P2P networking and chain-processing threads sharing the same process.

### Likelihood Explanation
The default bind address is `127.0.0.1:8114`, which limits exposure to local processes. However, a significant fraction of operators expose the RPC port publicly (required for remote miners, block explorers, and dApps). Any process or user on the same host can exploit this even with the default binding. The exploit requires no authentication, no PoW, and no special privileges — a single `curl` command suffices.

### Recommendation
Pass `config.max_request_body_size` into `start_server` and apply `tower_http::limit::RequestBodyLimitLayer` (already an available transitive dependency via `tower-http`) as the outermost layer of the axum `Router`:

```rust
use tower_http::limit::RequestBodyLimitLayer;

// in start_server, accept max_body: usize
let app = Router::new()
    // ... routes ...
    .layer(RequestBodyLimitLayer::new(max_body))
    .layer(CorsLayer::permissive())
    .layer(TimeoutLayer::with_status_code(...));
```

`RequestBodyLimitLayer` must be placed **before** (i.e., wrapping) the handler layers so it intercepts the body stream before axum buffers it.

### Proof of Concept
```bash
# Against a node with RPC exposed (or from localhost)
dd if=/dev/zero bs=1M count=500 | \
  curl -s -X POST http://127.0.0.1:8114 \
       -H 'Content-Type: application/json' \
       --data-binary @- &

# Monitor RSS of the ckb process
watch -n0.5 'ps -o rss= -p $(pgrep ckb)'
```
Expected: RSS climbs by ~500 MB per concurrent request; the node does not reject the body before buffering it. With the fix applied, the server returns HTTP 413 after `max_request_body_size` bytes are received.

### Citations

**File:** rpc/src/server.rs (L59-64)
```rust
        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
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

**File:** rpc/src/server.rs (L218-221)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
```

**File:** rpc/src/server.rs (L231-238)
```rust
    let req = match std::str::from_utf8(req_body.as_ref()) {
        Ok(req) => req,
        Err(_) => {
            return make_error_response(jsonrpc_core::Error::parse_error());
        }
    };

    let req = serde_json::from_str::<Request>(req);
```

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```
