The code confirms all claims. Let me verify the key facts:

1. `RpcServer::new` passes only `config.listen_address` to `start_server` — `config.max_request_body_size` is silently dropped. [1](#0-0) 

2. `start_server` signature has no size parameter. [2](#0-1) 

3. The axum `Router` is built with only `CorsLayer` and `TimeoutLayer` — no body size limit middleware. [3](#0-2) 

4. `handle_jsonrpc` uses `req_body: Bytes` as a direct axum extractor, meaning axum buffers the entire body before the handler runs. [4](#0-3) 

5. TCP path applies `LinesCodec::new_with_max_length(2 * 1024 * 1024)`, confirming the pattern was known and applied there but omitted for HTTP. [5](#0-4) 

6. `max_request_body_size` is defined in the config struct but never consumed in `server.rs`. [6](#0-5) 

All claims are code-confirmed. The finding is valid.

---

Audit Report

## Title
`max_request_body_size` Never Enforced — Unbounded HTTP Body Enables Node OOM Crash — (`rpc/src/server.rs`)

## Summary
`RpcConfig::max_request_body_size` is declared in the config struct and defaulted in `ckb.toml`, but `RpcServer::new` never passes it to `start_server`, and the axum `Router` is assembled with no body-size-limiting middleware. Any client that can reach the RPC port can POST an arbitrarily large body, forcing the node to buffer the entire payload in memory and triggering an OOM kill of the node process.

## Finding Description
In `RpcServer::new` (`rpc/src/server.rs`, lines 59–64), `start_server` is called with only `config.listen_address`; `config.max_request_body_size` is silently dropped. `start_server`'s signature (`lines 97–102`) accepts no size parameter. The axum `Router` (`lines 119–129`) is built with only `CorsLayer` and `TimeoutLayer` — no `tower_http::limit::RequestBodyLimitLayer` or equivalent. `handle_jsonrpc` (`lines 218–221`) extracts `req_body: Bytes` as a direct axum extractor, meaning axum reads and buffers the **entire** request body into memory before the handler is invoked. There is no check at any layer between TCP accept and handler invocation that limits body size. By contrast, the TCP path (`line 165`) correctly applies `LinesCodec::new_with_max_length(2 * 1024 * 1024)`, confirming the pattern was known and intentionally applied there but omitted for HTTP. Operators who set `max_request_body_size` in `ckb.toml` believing it protects them receive no protection.

## Impact Explanation
A single HTTP POST with a multi-gigabyte body causes axum to buffer the raw bytes, then `serde_json` allocates a second copy during parsing, and `JsonBytes` deserialization allocates a third buffer for decoded binary. For a 2 GiB hex string this is ~5 GiB peak RSS, OOM-killing the entire `ckb` node process and halting block production and sync. This is a full node process crash, not merely an RPC handler error, matching **High — Vulnerability which could easily crash a CKB node**.

## Likelihood Explanation
The RPC defaults to `127.0.0.1:8114`, limiting default exposure to local processes. However, many operators expose the RPC to LAN or the public internet (the config warns against it but does not prevent it). Any local process — a compromised miner client, a malicious script — can trigger this without credentials. Critically, the operator has no effective mitigation: the config knob they would reach for (`max_request_body_size`) silently does nothing, leaving the node unprotected even when operators believe they have configured a limit.

## Recommendation
In `RpcServer::new`, pass `config.max_request_body_size` into `start_server` as an additional `usize` parameter. Inside `start_server`, apply `tower_http::limit::RequestBodyLimitLayer::new(max_request_body_size)` to the axum `Router`, mirroring the `LinesCodec::new_with_max_length` guard already present on the TCP path.

## Proof of Concept
```bash
# Generate a 4 GiB hex payload and POST it to the RPC endpoint
python3 -c "
import sys
sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"send_transaction\",\"params\":[{\"version\":\"0x0\",\"cell_deps\":[],\"header_deps\":[],\"inputs\":[],\"outputs\":[],\"outputs_data\":[],\"witnesses\":[\"0x')
sys.stdout.flush()
chunk = '00' * (1024 * 1024)
for _ in range(2048):
    sys.stdout.write(chunk)
sys.stdout.write('\"]}]}')
" | curl -s -X POST http://127.0.0.1:8114 \
     -H 'Content-Type: application/json' \
     --data-binary @-
# Expected: node OOM-killed before any response is sent.
# The max_request_body_size = 10485760 guard in ckb.toml is never applied.
```

### Citations

**File:** rpc/src/server.rs (L59-64)
```rust
        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
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
