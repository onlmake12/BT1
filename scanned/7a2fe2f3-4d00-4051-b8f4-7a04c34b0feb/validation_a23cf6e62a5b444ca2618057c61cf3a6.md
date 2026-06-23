### Title
`max_request_body_size` Config Field Is Declared But Never Enforced in the HTTP/WS RPC Server — (`File: rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server declares a `max_request_body_size` configuration field and documents a 10 MiB default, but the value is **never wired into the axum middleware stack**. The HTTP handler buffers the entire incoming request body into memory before any application logic runs, with no enforced upper bound. Any RPC caller who can reach the endpoint can send an arbitrarily large POST body, causing unbounded heap allocation and potential OOM-kill of the node process.

---

### Finding Description

`RpcConfig` (aliased as `RpcConfig` in the server) carries a `max_request_body_size: usize` field: [1](#0-0) 

The default config files document and set this to 10 MiB: [2](#0-1) 

However, in `RpcServer::new`, only `config.rpc_batch_limit` and the listen addresses are consumed from the config. The `max_request_body_size` value is **silently dropped** — it is never passed to `start_server`: [3](#0-2) 

Inside `start_server`, the axum `Router` is assembled with only `CorsLayer` and `TimeoutLayer`. There is no `tower_http::limit::RequestBodyLimitLayer` or `axum::extract::DefaultBodyLimit` applied anywhere: [4](#0-3) 

The JSON-RPC handler receives the body as a fully-buffered `Bytes` value. Axum's default body limit is effectively unlimited, so the entire attacker-controlled payload is read into heap memory before the handler is invoked: [5](#0-4) 

The same server code is reused for both the HTTP and WebSocket endpoints: [6](#0-5) 

The TCP transport does apply a codec limit of 2 MiB per line, so it is not affected: [7](#0-6) 

---

### Impact Explanation

An attacker who can reach the RPC HTTP port sends concurrent POST requests with multi-gigabyte bodies (e.g., a single JSON field padded to several GB). Axum buffers each body fully before dispatching to the handler. With several concurrent connections, heap usage grows until the OS OOM-killer terminates the `ckb` process. This crashes the node, halting block production, transaction relay, and all RPC services. The documented `max_request_body_size` limit gives operators a false sense of protection — they believe a 10 MiB cap is active when none is enforced.

---

### Likelihood Explanation

The RPC server defaults to `127.0.0.1:8114` (localhost only), which limits exposure on a default installation. However, operators running public nodes, mining pools, or exchanges routinely expose the RPC port to internal networks or the internet. The attack requires only the ability to make an HTTP POST to the RPC port — no authentication, no special protocol knowledge, and no cryptographic material. The attacker-controlled entry path is a standard HTTP POST to any RPC method (e.g., `send_transaction` with a large `witnesses` field).

---

### Recommendation

Apply `tower_http::limit::RequestBodyLimitLayer` (already a transitive dependency via `tower-http`) in `start_server`, using the `max_request_body_size` value from the config:

```rust
use tower_http::limit::RequestBodyLimitLayer;

let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(RequestBodyLimitLayer::new(max_request_body_size))  // add this
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
```

`start_server` must be updated to accept `max_request_body_size: usize` as a parameter, and `RpcServer::new` must pass `config.max_request_body_size` through.

---

### Proof of Concept

```python
import grequests

url = "http://127.0.0.1:8114/"
payload = {
    "id": 1,
    "jsonrpc": "2.0",
    "method": "send_transaction",
    "params": [{"version": "0x0", "cell_deps": [], "header_deps": [],
                 "inputs": [], "outputs": [],
                 "outputs_data": ["0x" + "aa" * 500_000_000],  # ~500 MB per request
                 "witnesses": []}]
}
rs = [grequests.post(url, json=payload) for _ in range(8)]
grequests.map(rs)
```

Each concurrent request causes axum to allocate the full body on the heap before the handler runs. With 8 concurrent 500 MB requests, ~4 GB of heap is consumed, triggering an OOM-kill of the `ckb` process.

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

**File:** rpc/src/server.rs (L218-221)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
```
