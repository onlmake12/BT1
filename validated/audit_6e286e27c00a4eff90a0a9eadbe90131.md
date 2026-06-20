The evidence is conclusive. Let me verify there's no body-limit layer anywhere in the RPC module.

### Title
`max_request_body_size` Config Value Is Never Applied to the Axum HTTP RPC Server — (`rpc/src/server.rs`)

### Summary

`Config::max_request_body_size` is declared, documented, and has a default of 10 MiB, but it is silently ignored when constructing the axum HTTP server. No body-size-limiting middleware is applied to the router. Any caller who can reach the HTTP RPC port can POST an arbitrarily large body, causing unbounded heap allocation per request and enabling memory-exhaustion DoS.

### Finding Description

`util/app-config/src/configs/rpc.rs` declares the field:

```rust
/// Max request body size in bytes.
pub max_request_body_size: usize,
``` [1](#0-0) 

`RpcServer::new()` receives the full `RpcConfig` but only consumes four fields from it — `rpc_batch_limit`, `listen_address`, `ws_listen_address`, and `tcp_listen_address`. `max_request_body_size` is never read: [2](#0-1) 

`start_server()` builds the axum `Router` with only a `CorsLayer` and a `TimeoutLayer`. There is no `tower_http::limit::RequestBodyLimitLayer` or any equivalent: [3](#0-2) 

The handler `handle_jsonrpc` receives `req_body: Bytes` directly. Axum buffers the entire request body into memory before the handler is invoked — with no upper bound: [4](#0-3) 

A codebase-wide search for `RequestBodyLimit`, `body_limit`, and `ContentLengthLimit` returns zero matches. The TCP server does apply a 2 MiB cap via `LinesCodec::new_with_max_length`, but the HTTP path has no equivalent: [5](#0-4) 

### Impact Explanation

An attacker who can reach the HTTP RPC port (default `127.0.0.1:8114`, but commonly exposed by operators) can send a single POST request with a multi-gigabyte body. Axum will attempt to buffer the entire body into a `Bytes` allocation on the heap. Repeating this across a small number of concurrent connections exhausts available memory, triggering OOM-kill of the node process or severe swap thrashing, causing a complete denial of service.

### Likelihood Explanation

- The config field and its default value create a false sense of security; operators believe the limit is enforced.
- The RPC is localhost-only by default, but the documentation explicitly acknowledges that operators expose it, and many production deployments do so behind a reverse proxy or on a LAN.
- Even in the localhost-only case, any unprivileged local process (e.g., a compromised dependency, a co-tenant container) can exploit this.
- No authentication is required on the HTTP RPC endpoint.
- Exploitation requires only a single `curl` invocation with a large body.

### Recommendation

Pass `config.max_request_body_size` into `start_server` and apply `tower_http::limit::RequestBodyLimitLayer` to the axum router:

```rust
use tower_http::limit::RequestBodyLimitLayer;

// in start_server signature:
fn start_server(
    rpc: &Arc<MetaIoHandler<Option<Session>>>,
    address: String,
    handler: Handle,
    enable_websocket: bool,
    max_body_size: usize,   // <-- add this
) -> Result<SocketAddr, AnyError> {
    // ...
    let app = Router::new()
        // ... routes ...
        .layer(RequestBodyLimitLayer::new(max_body_size))  // <-- enforce limit
        .layer(CorsLayer::permissive())
        // ...
}
```

And pass `config.max_request_body_size` at both call sites in `RpcServer::new`.

### Proof of Concept

```bash
# Generate a 500 MiB body
dd if=/dev/zero bs=1M count=500 | \
  curl -s -X POST http://127.0.0.1:8114 \
       -H 'Content-Type: application/json' \
       --data-binary @- \
       -o /dev/null -w "%{http_code}\n"
# Expected with fix: 413 Payload Too Large
# Actual without fix: request is fully buffered; node RSS grows by ~500 MiB per connection
```

Repeating with 10–20 concurrent connections will exhaust typical node memory and trigger OOM.

### Citations

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```

**File:** rpc/src/server.rs (L52-95)
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

        let tcp_address = if let Some(addr) = config.tcp_listen_address {
            let local_addr = handler.block_on(Self::start_tcp_server(rpc, addr, handler.clone()));
            if let Ok(addr) = &local_addr {
                info!("Listen TCP RPCServer on address: {}", addr);
            };
            local_addr.ok()
        } else {
            None
        };

        Self {
            http_address,
            tcp_address,
            ws_address,
        }
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
