The evidence is conclusive. `max_request_body_size` is defined in the config struct and set to 10 MiB in the default `ckb.toml`, but it is **never passed to or applied in `start_server`**. The axum router has no `RequestBodyLimitLayer` or equivalent middleware. The config value is a dead letter.

---

### Title
`max_request_body_size` Config Value Is Never Enforced — Unbounded HTTP Request Body Causes OOM — (`rpc/src/server.rs`)

### Summary
The `RpcConfig::max_request_body_size` field is documented, defaulted to 10 MiB, and operators rely on it as a guard. However, `start_server` never receives the config and the axum `Router` is built with no body-size-limiting middleware. Any caller who can reach the RPC port can send an arbitrarily large HTTP body, forcing the node to buffer the entire payload in memory before any application-level validation runs.

### Finding Description

`RpcServer::new` reads `config.max_request_body_size` but passes only `config.listen_address` to `start_server`: [1](#0-0) 

`start_server`'s signature accepts no size parameter: [2](#0-1) 

The axum `Router` is assembled with only `CorsLayer` and `TimeoutLayer` — no `tower_http::limit::RequestBodyLimitLayer` or any equivalent: [3](#0-2) 

`handle_jsonrpc` receives `req_body: Bytes`, meaning axum has already read and buffered the **entire** body before the handler is invoked: [4](#0-3) 

Only after that full buffer exists does `BytesVisitor::visit_str` allocate a second buffer of `hex_len / 2` bytes for decoding: [5](#0-4) 

A grep across the entire repository confirms `max_request_body_size` is referenced only in the config struct definition and a test fixture — never in the server implementation: [6](#0-5) 

The default config documents the intended 10 MiB ceiling: [7](#0-6) 

But that value is never wired into the server.

### Impact Explanation
A single HTTP POST with a multi-gigabyte hex-encoded `JsonBytes` field (e.g., a witness in `send_transaction`) causes:
1. axum buffers the full body (N bytes of JSON string) — first allocation.
2. `serde_json` parses the string token — second allocation of the same size.
3. `BytesVisitor::visit_str` allocates `N/2` bytes for the decoded binary — third allocation.

For a 2 GiB hex string this is ~5 GiB of peak RSS, OOM-killing the node process and halting block production and sync.

### Likelihood Explanation
The RPC defaults to `127.0.0.1` only, which limits exposure to local processes. However:
- Many operators expose the RPC to LAN or the public internet (the config warns against it but does not prevent it).
- Any local process (e.g., a compromised miner client, a malicious script) can trigger this without any credentials.
- The operator has no effective mitigation because the config knob they would reach for (`max_request_body_size`) silently does nothing.

### Recommendation
In `RpcServer::new`, pass `config.max_request_body_size` into `start_server` and apply `tower_http::limit::RequestBodyLimitLayer::new(max_request_body_size)` to the axum `Router`. The TCP path already applies an analogous limit via `LinesCodec::new_with_max_length(2 * 1024 * 1024)`: [8](#0-7) 

The HTTP path must receive the same treatment.

### Proof of Concept
```bash
# Generate a 2 GiB hex payload (4 GiB of '0' characters + "0x" prefix)
python3 -c "
import sys
sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"send_transaction\",\"params\":[{\"version\":\"0x0\",\"cell_deps\":[],\"header_deps\":[],\"inputs\":[],\"outputs\":[],\"outputs_data\":[],\"witnesses\":[\"0x')
sys.stdout.flush()
# write 4 GiB of hex zeros
chunk = '00' * (1024 * 1024)  # 2 MiB of hex
for _ in range(2048):          # 2048 * 2 MiB = 4 GiB hex = 2 GiB decoded
    sys.stdout.write(chunk)
sys.stdout.write('\"]}]}')
" | curl -s -X POST http://127.0.0.1:8114 \
     -H 'Content-Type: application/json' \
     --data-binary @-
# Expected: node process OOM-killed before any response is sent.
# Actual guard (max_request_body_size = 10485760) is never applied.
```

### Citations

**File:** rpc/src/server.rs (L59-68)
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
```

**File:** rpc/src/server.rs (L97-102)
```rust
    fn start_server(
        rpc: &Arc<MetaIoHandler<Option<Session>>>,
        address: String,
        handler: Handle,
        enable_websocket: bool,
    ) -> Result<SocketAddr, AnyError> {
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

**File:** util/jsonrpc-types/src/bytes.rs (L104-106)
```rust
        let mut buffer = vec![0; bytes.len() >> 1]; // we checked length
        hex_decode(bytes, &mut buffer).map_err(|e| E::custom(format_args!("{e:?}")))?;
        Ok(JsonBytes::from_vec(buffer))
```

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
