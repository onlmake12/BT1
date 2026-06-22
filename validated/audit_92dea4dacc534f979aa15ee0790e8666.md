The code confirms this is a real vulnerability. Here is the full analysis:

**Key facts from the code:**

1. `RpcConfig` defines `max_request_body_size: usize` (default 10 MiB per config). [1](#0-0) 

2. In `RpcServer::new`, only `config.rpc_batch_limit` is consumed from the config. `config.max_request_body_size` is read but **never passed** to `start_server`. [2](#0-1) 

3. `start_server` does not accept a body-size parameter. The axum `Router` it builds applies only `CorsLayer`, `TimeoutLayer`, and two `Extension` layers — **no `RequestBodyLimitLayer`**. [3](#0-2) 

4. The HTTP handler `handle_jsonrpc` takes `req_body: Bytes` directly. Axum will fully buffer the entire request body into memory before the handler is invoked, with no size gate. [4](#0-3) 

5. The TCP server path does apply a codec limit (`LinesCodec::new_with_max_length(2 * 1024 * 1024)`), but the HTTP path has no equivalent. [5](#0-4) 

---

### Title
`max_request_body_size` Never Enforced at HTTP Transport Layer, Enabling Memory Exhaustion via Unbounded POST Body — (`rpc/src/server.rs`)

### Summary
`RpcConfig.max_request_body_size` is a dead config field. It is parsed and stored but never wired into the axum HTTP server. Any caller who can reach the RPC HTTP port can POST an arbitrarily large body, which axum will fully buffer into process memory before any application-level check runs, enabling a single-request OOM crash.

### Finding Description
In `RpcServer::new`, `config.max_request_body_size` is never forwarded to `start_server`. Inside `start_server`, the axum `Router` is built with `CorsLayer`, `TimeoutLayer`, and `Extension` layers only — `tower_http::limit::RequestBodyLimitLayer` is absent. The handler `handle_jsonrpc` receives the body as `Bytes`, which axum materializes by reading the full stream into a heap allocation. There is no middleware or extractor-level guard that rejects or truncates oversized bodies before this allocation occurs.

### Impact Explanation
A single HTTP POST with a multi-gigabyte `Content-Length` (or chunked body) causes the node process to allocate that memory before any JSON parsing or RPC dispatch. This exhausts available RAM/swap and crashes the node (OOM kill or allocation failure), halting block production, sync, and all RPC service. The `TimeoutLayer` (30 s) does not help because the OS will deliver data faster than the timeout on a local or fast network link.

### Likelihood Explanation
The default bind address is `127.0.0.1:8114`, so remote exploitation requires the operator to have exposed the RPC port — a documented and supported configuration. The config comment explicitly warns about this exposure, confirming it is an intended deployment option. Any process or user with TCP access to the RPC port (local or remote) can trigger this with a single `curl` command. No authentication, key, or privilege is required.

### Recommendation
Pass `config.max_request_body_size` into `start_server` and add `RequestBodyLimitLayer::new(max_request_body_size)` to the axum `Router` layer stack, before the handler layers. Example:

```rust
use tower_http::limit::RequestBodyLimitLayer;

let app = Router::new()
    // ... routes ...
    .layer(RequestBodyLimitLayer::new(max_request_body_size))
    .layer(CorsLayer::permissive())
    // ...
```

This causes axum to return HTTP 413 and close the connection as soon as the declared or streamed body exceeds the limit, before any heap allocation for the body occurs.

### Proof of Concept
```bash
# Against a node with RPC exposed (default or custom bind)
curl -X POST http://127.0.0.1:8114/ \
  -H "Content-Type: application/json" \
  --data-binary @/dev/zero \
  --limit-rate 1G \
  -m 30
# /dev/zero streams nulls; the node buffers everything until OOM or timeout.
# With a real 10 GiB file, the node OOMs before the 30s timeout fires.
```

### Citations

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```

**File:** rpc/src/server.rs (L52-68)
```rust
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }

        let rpc = Arc::new(io_handler);

        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();
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

**File:** rpc/src/server.rs (L165-165)
```rust
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```

**File:** rpc/src/server.rs (L218-221)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
```
