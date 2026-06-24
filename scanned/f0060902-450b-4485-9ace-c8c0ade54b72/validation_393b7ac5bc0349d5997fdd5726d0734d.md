Audit Report

## Title
`max_request_body_size` Config Value Is Never Enforced in the HTTP/WebSocket RPC Server, Allowing Unbounded Memory Consumption via Oversized Requests - (File: `rpc/src/server.rs`)

## Summary
The CKB node declares a `max_request_body_size` field in `RpcConfig` (defaulting to 10 MiB in `ckb.toml`) but never passes or applies it to the axum HTTP/WebSocket server. The `start_server` function assembles the axum `Router` with only `CorsLayer` and `TimeoutLayer` — no body-size-limiting middleware — so the configured limit is silently ignored. An attacker who can reach the RPC port can POST arbitrarily large request bodies, causing axum to buffer the entire payload into heap memory before `handle_jsonrpc` is invoked, leading to unbounded memory growth and potential OOM-kill of the node process.

## Finding Description
`RpcConfig` declares `max_request_body_size: usize` at `util/app-config/src/configs/rpc.rs:40`, and `resource/ckb.toml:187` sets it to `10485760` (10 MiB). However, in `RpcServer::new()` (`rpc/src/server.rs:52-78`), when `start_server` is called for both HTTP and WebSocket endpoints, only `address`, `handler`, and `enable_websocket` are forwarded — `max_request_body_size` is never passed.

The `start_server` function signature (`rpc/src/server.rs:97-102`) accepts no body-size parameter, and the axum `Router` built at lines 119-129 applies only `CorsLayer::permissive()` and `TimeoutLayer` — no `RequestBodyLimitLayer` or equivalent. The `handle_jsonrpc` handler at line 220 receives `req_body: Bytes`, meaning axum fully buffers the entire request body into a heap allocation before the handler is invoked, with no enforced cap.

By contrast, the TCP RPC server does apply a per-line limit of 2 MiB via `LinesCodec::new_with_max_length(2 * 1024 * 1024)` at `rpc/src/server.rs:165`, confirming the HTTP/WebSocket path is the unprotected outlier.

Additionally, `rpc_batch_limit` is `None` by default (commented out at `resource/ckb.toml:208`), so the batch-size guard at `rpc/src/server.rs:275-282` is also inactive by default, compounding the risk.

A codebase-wide search for `RequestBodyLimit`, `body_limit`, or `BodyLimit` returns zero matches, confirming no body size enforcement exists anywhere in the HTTP/WebSocket RPC stack.

## Impact Explanation
**High — Vulnerabilities which could easily crash a CKB node.** An attacker who can reach the RPC HTTP port sends one or more POST requests with multi-gigabyte JSON bodies. The axum runtime buffers each body fully into heap memory before `handle_jsonrpc` is called. Sending several such requests concurrently exhausts available RAM, triggering an OOM-kill of the `ckb` process. This terminates all node services: block relay, sync, transaction pool, and RPC — a complete denial of service requiring manual restart.

## Likelihood Explanation
**Medium.** By default the RPC binds to `127.0.0.1:8114`, restricting direct internet exposure. However, many operators expose the RPC port to broader networks for dApp backends, mining pools, or monitoring — explicitly warned against but commonly done. The WebSocket endpoint (`ws_listen_address`) is served by the same unprotected `start_server` path. Any local process on the same machine (e.g., a compromised dependency or co-located service) can reach `127.0.0.1:8114` without authentication. The attack requires only a standard HTTP client and a large payload — no special knowledge of CKB internals.

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

Each concurrent request causes axum to allocate ~500 MB on the heap before `handle_jsonrpc` is reached. Eight concurrent requests attempt to allocate ~4 GB, exhausting memory on typical node hardware and triggering an OOM-kill of the `ckb` process. The root cause is directly verifiable by reading `rpc/src/server.rs:97-129` — `start_server` has no body-size parameter and the router has no limiting layer.