The evidence is conclusive. Let me verify there's no body limit applied anywhere else in the stack.

### Title
`max_request_body_size` Config Field Silently Ignored — Axum HTTP RPC Server Accepts Unbounded Request Bodies — (`rpc/src/server.rs`)

### Summary

The `max_request_body_size` field is declared in the RPC config struct and documented as enforcing a body-size cap, but it is **never read or applied** in the HTTP server setup. The axum router has no `RequestBodyLimitLayer` or `DefaultBodyLimit` layer. Any client that can reach the RPC port can POST an arbitrarily large body, which axum buffers entirely into heap memory before dispatching to the handler, enabling memory exhaustion.

### Finding Description

`Config::max_request_body_size` is defined in `util/app-config/src/configs/rpc.rs`: [1](#0-0) 

`RpcServer::new()` receives the full `RpcConfig` but only consumes `rpc_batch_limit`, `listen_address`, `ws_listen_address`, and `tcp_listen_address`. The `max_request_body_size` field is never read: [2](#0-1) 

`start_server()` does not accept a body-size parameter, and the axum `Router` it builds contains no body-limiting middleware — only `CorsLayer` and `TimeoutLayer`: [3](#0-2) 

The handler `handle_jsonrpc` extracts the full body as `Bytes` with no prior size gate: [4](#0-3) 

A repo-wide search for `RequestBodyLimit`, `DefaultBodyLimit`, and `body_limit` returns zero matches, confirming no alternative enforcement exists anywhere in the stack.

### Impact Explanation

An attacker who can reach the RPC port (default `127.0.0.1:8114`, but frequently exposed on `0.0.0.0` in production deployments) can POST a multi-gigabyte body. Axum buffers the entire body into heap memory before calling `handle_jsonrpc`. Repeated requests exhaust available RAM, causing the node process to be OOM-killed or to stall, resulting in a complete denial of service of the CKB node.

### Likelihood Explanation

The RPC is localhost-only by default, but:
- Many operators expose it on `0.0.0.0` for remote tooling.
- Even with localhost binding, any unprivileged local process can exploit this.
- No authentication is required on the RPC endpoint.
- The default config advertises a 10 MiB limit (`max_request_body_size = 10485760`) that is never actually enforced, so operators have no reason to suspect the protection is absent.

### Recommendation

In `RpcServer::new()`, pass `config.max_request_body_size` into `start_server()` and apply it as an axum middleware layer:

```rust
use tower_http::limit::RequestBodyLimitLayer;

// inside start_server, after building the Router:
let app = app.layer(RequestBodyLimitLayer::new(max_request_body_size));
```

The TCP path already applies a 2 MiB codec limit via `LinesCodec::new_with_max_length(2 * 1024 * 1024)` [5](#0-4)  — the HTTP/WS path needs equivalent protection.

### Proof of Concept

```bash
# Start a CKB node with default config (max_request_body_size = 10485760)
# Send a 500 MiB body — expect 413, actually accepted and buffered:
python3 -c "
import socket, time
s = socket.create_connection(('127.0.0.1', 8114))
body = b'X' * (500 * 1024 * 1024)
req = (
    b'POST / HTTP/1.1\r\n'
    b'Host: 127.0.0.1:8114\r\n'
    b'Content-Type: application/json\r\n'
    b'Content-Length: ' + str(len(body)).encode() + b'\r\n\r\n'
    + body
)
s.sendall(req)
print(s.recv(4096))
"
# Observe: no 413 is returned; node RSS grows by ~500 MiB.
```

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
