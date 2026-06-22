The investigation is complete. Here is the full analysis:

**Key findings:**

1. `max_request_body_size` is defined in `RpcConfig` and documented as "Default is 10MiB" in the config files, but it is **never applied** in `rpc/src/server.rs`. [1](#0-0) 

2. `RpcServer::new()` receives `config: RpcConfig` but only uses `rpc_batch_limit`, `listen_address`, `ws_listen_address`, and `tcp_listen_address` — `max_request_body_size` is silently ignored. [1](#0-0) 

3. There is no `RequestBodyLimitLayer`, `DefaultBodyLimit`, or any other body-size-limiting middleware anywhere in the codebase. The grep for `RequestBodyLimit|body_limit|DefaultBodyLimit` returns zero matches. [2](#0-1) 

4. The HTTP handler receives `req_body: Bytes` with no size guard before passing to `serde_json::from_str::<Request>`. [3](#0-2) 

5. The TCP server does apply a 2 MB codec limit, but the HTTP server does not. [4](#0-3) 

6. `ProposalShortId` deserialization allocates a fixed 10-byte array per entry with no array-length cap. [5](#0-4) 

---

### Title
`max_request_body_size` Config Never Enforced — Unbounded HTTP Body Allows OOM via `submit_block` Proposals Array — (`rpc/src/server.rs`)

### Summary
The `max_request_body_size` RPC configuration option is documented and defaults to 10 MiB, but it is never wired into the axum HTTP server as a middleware layer. Any caller with HTTP RPC access can POST an arbitrarily large body. A `submit_block` request with millions of `proposals` entries causes unbounded `Vec<ProposalShortId>` allocation during serde deserialization, exhausting process memory before any consensus-level check is reached.

### Finding Description
`RpcServer::new()` in `rpc/src/server.rs` reads `config.max_request_body_size` from `RpcConfig` but never passes it to the axum router. No `tower_http::limit::RequestBodyLimitLayer` or `axum::extract::DefaultBodyLimit` is applied anywhere. The `handle_jsonrpc` handler extracts `req_body: Bytes` — axum's default body extractor streams the entire request body into memory with no cap. After buffering, `serde_json::from_str::<Request>` deserializes the JSON, which for a `submit_block` call with N proposals allocates a `Vec<ProposalShortId>` of N × 10 bytes plus the JSON string itself (~24 bytes per entry). At 10 million entries this is ~340 MB per request; at 100 million entries it exceeds 3 GB.

The TCP RPC path does apply a 2 MB `LinesCodec` limit, making the HTTP path the only unguarded surface.

### Impact Explanation
An attacker with HTTP RPC access can crash the CKB node process via OOM. On Linux, the OOM killer terminates the process; on other platforms, Rust's allocator panics. Either outcome terminates the node, halting block production, transaction relay, and all RPC services until manually restarted. Impact is scoped to local RPC API crash (Note, 0–500 points).

### Likelihood Explanation
The RPC binds to `127.0.0.1:8114` by default, so exploitation requires local access or an operator who has exposed the port. The config file explicitly warns against network exposure, but the `max_request_body_size` field gives operators a false sense of protection — it is documented as active but is dead code. Any local process (e.g., a compromised co-located service) can trigger this without credentials.

### Recommendation
Apply `axum::extract::DefaultBodyLimit::max(config.max_request_body_size)` as a layer on the axum router in `RpcServer::start_server`, or use `tower_http::limit::RequestBodyLimitLayer::new(config.max_request_body_size)`. This enforces the limit before the body is buffered into `Bytes`, returning HTTP 413 for oversized requests.

### Proof of Concept
```python
import socket, time

# 10M proposals × 24 bytes each ≈ 240 MB JSON body
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
# Node process OOM-killed or panics; no HTTP 413 is returned
time.sleep(5)
print(s.recv(4096))
```

### Citations

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

**File:** util/jsonrpc-types/src/proposal_short_id.rs (L61-72)
```rust
    fn visit_str<E>(self, v: &str) -> Result<Self::Value, E>
    where
        E: serde::de::Error,
    {
        if v.len() < 2 || &v.as_bytes()[0..2] != b"0x" || v.len() != 22 {
            return Err(E::invalid_value(serde::de::Unexpected::Str(v), &self));
        }
        let mut buffer = [0u8; 10]; // we checked length
        hex_decode(&v.as_bytes()[2..], &mut buffer)
            .map_err(|e| E::custom(format_args!("{e:?}")))?;
        Ok(ProposalShortId::new(buffer))
    }
```
