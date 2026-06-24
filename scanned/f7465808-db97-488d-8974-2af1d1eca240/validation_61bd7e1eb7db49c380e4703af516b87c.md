Audit Report

## Title
`max_request_body_size` Config Value Never Enforced — Unbounded HTTP Request Body Enables Node OOM Crash — (`rpc/src/server.rs`)

## Summary
`RpcConfig::max_request_body_size` is defined in the config struct and defaulted to 10 MiB in `ckb.toml`, but `RpcServer::new` never passes this value to `start_server`, and the axum `Router` is assembled with no body-size-limiting middleware. Any client that can reach the RPC port can POST an arbitrarily large body, forcing the node to buffer the entire payload in memory and triggering an OOM kill.

## Finding Description
`RpcServer::new` calls `start_server` passing only `config.listen_address`; `config.max_request_body_size` is silently dropped: [1](#0-0) 

`start_server`'s signature accepts no size parameter: [2](#0-1) 

The axum `Router` is built with only `CorsLayer` and `TimeoutLayer` — no `tower_http::limit::RequestBodyLimitLayer` or any equivalent guard: [3](#0-2) 

`handle_jsonrpc` extracts `req_body: Bytes` as a direct axum extractor, meaning axum has already read and buffered the **entire** request body into memory before the handler is invoked: [4](#0-3) 

A grep across the entire repository confirms `max_request_body_size` is referenced only in the config struct definition, `CHANGELOG.md`, `ckb.toml`, and a test fixture — never in `server.rs`: [5](#0-4) 

By contrast, the TCP path correctly applies an analogous limit via `LinesCodec::new_with_max_length(2 * 1024 * 1024)`, demonstrating the pattern was known and intentionally applied there but omitted for HTTP: [6](#0-5) 

## Impact Explanation
A single HTTP POST with a multi-gigabyte body (e.g., a hex-encoded witness field in `send_transaction`) causes three sequential memory allocations: axum buffers the raw body, `serde_json` allocates a second copy during parsing, and `JsonBytes` deserialization allocates a third buffer of `body_len / 2` for the decoded binary. For a 2 GiB hex string this is ~5 GiB peak RSS, OOM-killing the node process and halting block production and sync. This matches the allowed bounty impact: **High — Vulnerability which could easily crash a CKB node**.

## Likelihood Explanation
The RPC defaults to `127.0.0.1:8114`, limiting default exposure to local processes. However, many operators expose the RPC to LAN or the public internet (the config warns against it but does not prevent it). Any local process — a compromised miner client, a malicious script — can trigger this without credentials. Critically, the operator has no effective mitigation: the config knob they would reach for (`max_request_body_size`) silently does nothing, so the node remains unprotected even when operators believe they have configured a limit.

## Recommendation
In `RpcServer::new`, pass `config.max_request_body_size` into `start_server` as an additional parameter, then apply `tower_http::limit::RequestBodyLimitLayer::new(max_request_body_size as usize)` to the axum `Router` in `start_server`, mirroring the `LinesCodec::new_with_max_length` guard already present on the TCP path. [3](#0-2) 

## Proof of Concept
```bash
# Generate a 2 GiB hex payload and POST it to the RPC endpoint
python3 -c "
import sys
sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"send_transaction\",\"params\":[{\"version\":\"0x0\",\"cell_deps\":[],\"header_deps\":[],\"inputs\":[],\"outputs\":[],\"outputs_data\":[],\"witnesses\":[\"0x')
sys.stdout.flush()
chunk = '00' * (1024 * 1024)  # 2 MiB of hex chars
for _ in range(2048):          # 2048 * 2 MiB = 4 GiB hex = 2 GiB decoded
    sys.stdout.write(chunk)
sys.stdout.write('\"]}]}')
" | curl -s -X POST http://127.0.0.1:8114 \
     -H 'Content-Type: application/json' \
     --data-binary @-
# Expected: node OOM-killed before any response is sent.
# The max_request_body_size = 10485760 guard in ckb.toml is never applied.
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

**File:** util/app-config/src/configs/rpc.rs (L39-40)
```rust
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
```
