### Title
Unauthenticated `set_network_active` RPC Allows Any Caller to Fully Disable Node P2P Networking — (File: `rpc/src/module/net.rs`, `rpc/src/server.rs`)

### Summary

The CKB RPC server exposes a `set_network_active` method that acts as a network-layer "pause switch," capable of silently discarding all incoming and outgoing P2P messages. The RPC server is built with no authentication or authorization middleware whatsoever — any caller who can reach the RPC port can invoke this method. This is the direct analog of the reported vulnerability: a critical pause/unpause operation controlled by a single, unprotected access path with no role separation, no audit trail, and no accountability.

### Finding Description

**Root cause — no authentication on the RPC server:**

`rpc/src/server.rs` builds the Axum router with `CorsLayer::permissive()` and no authentication layer:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← all origins allowed, no auth
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
``` [1](#0-0) 

There is no bearer token, API key, IP allowlist, or any other access control enforced in code. The `handle_jsonrpc` dispatcher dispatches every well-formed JSON-RPC call directly to the handler with no identity check: [2](#0-1) 

**The privileged operation — `set_network_active`:**

The `Net` RPC module (enabled by default in `ckb.toml`) exposes `set_network_active`:

```rust
#[rpc(name = "set_network_active")]
fn set_network_active(&self, state: bool) -> Result<()>;
``` [3](#0-2) 

The implementation writes directly to the shared atomic flag with `Ordering::Release`:

```rust
pub fn set_active(&self, active: bool) {
    self.network_state.active.store(active, Ordering::Release);
}
``` [4](#0-3) 

When `active` is `false`, `is_active()` returns `false` and all received P2P messages are discarded: [5](#0-4) 

**Effect on the node:** Setting `active = false` causes the node to silently drop every inbound sync, relay, and discovery message. The node stops receiving new blocks and transactions from peers, stops propagating its own transactions, and falls out of consensus participation — all without any error or crash visible to the operator.

### Impact Explanation

Any caller who can reach the RPC port can issue:

```json
{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}
```

and the node immediately ceases all P2P communication. The node continues to appear "running" (process alive, RPC responsive) but is fully isolated from the network. Consequences include:

- **Consensus exclusion:** The node stops receiving new blocks; its chain tip stalls.
- **Transaction censorship:** Submitted transactions are never relayed to miners.
- **Silent degradation:** No alarm is raised; the operator may not notice for an extended period.
- **Persistent until manually reversed:** The flag persists in memory until `set_network_active(true)` is called or the node restarts.

The `Net` module is listed in the default `modules` array in `resource/ckb.toml`: [6](#0-5) 

### Likelihood Explanation

The default `listen_address` is `127.0.0.1:8114`, which limits exposure to local callers. However:

1. **No authentication exists in code at all** — the protection is entirely configuration-dependent. Operators who bind to `0.0.0.0` or expose the port via reverse proxy (a common operational pattern for monitoring/management) have zero code-level protection.
2. **`CorsLayer::permissive()`** means any web origin can issue cross-origin RPC calls if the port is reachable, enabling browser-based CSRF-style attacks against exposed nodes.
3. **Any local process** on the same host (including a compromised dependency, a co-located service, or a malicious script) can reach `127.0.0.1:8114` without any credential.
4. The TCP and WebSocket listeners (`tcp_listen_address`, `ws_listen_address`) are also supported and have the same zero-auth code path. [7](#0-6) 

### Recommendation

1. **Require an authentication token for state-mutating RPC methods.** Add a configurable `rpc_secret_token` to `RpcConfig`; require it as a header or query parameter for all non-read-only calls.
2. **Separate privileged operations into a distinct module** (e.g., `Admin`) that is disabled by default and requires explicit opt-in, analogous to the AccessControl pattern recommended in the report.
3. **Replace `CorsLayer::permissive()` with a strict allowlist** of trusted origins.
4. **Add an IP allowlist** enforced in the server middleware, not just in the bind address configuration.

### Proof of Concept

**Precondition:** CKB node running with default config (RPC on `127.0.0.1:8114`, `Net` module enabled).

**Step 1 — Disable all P2P networking:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}'
# Response: {"jsonrpc":"2.0","result":null,"id":1}
```

**Step 2 — Confirm node is network-isolated:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_peers","params":[],"id":2}'
# Peers remain listed but no new blocks are received; tip number stops advancing.
```

**Step 3 — Verify `is_active` flag:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"local_node_info","params":[],"id":3}'
```

The node's chain tip will stop advancing. No authentication was required at any step. The call succeeds identically whether issued by the legitimate operator or any other local or remote process that can reach the port.

### Citations

**File:** rpc/src/server.rs (L80-88)
```rust
        let tcp_address = if let Some(addr) = config.tcp_listen_address {
            let local_addr = handler.block_on(Self::start_tcp_server(rpc, addr, handler.clone()));
            if let Ok(addr) = &local_addr {
                info!("Listen TCP RPCServer on address: {}", addr);
            };
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

**File:** rpc/src/server.rs (L218-260)
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
    match req {
        Err(_error) => {
            let response = RpcResponse::from(
                Error::new(ErrorCode::ParseError),
                Some(jsonrpc_core::Version::V2),
            );

            serde_json::to_string(&response)
                .map(|json| {
                    (
                        [(axum::http::header::CONTENT_TYPE, "application/json")],
                        json,
                    )
                        .into_response()
                })
                .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
        Ok(request) => match request {
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;

                if let Some(response) = result {
```

**File:** rpc/src/module/net.rs (L419-420)
```rust
    #[rpc(name = "set_network_active")]
    fn set_network_active(&self, state: bool) -> Result<()>;
```

**File:** network/src/network.rs (L1583-1586)
```rust
    /// network message processing controller, always true, if false, discard any received messages
    pub fn is_active(&self) -> bool {
        self.network_state.is_active()
    }
```

**File:** network/src/network.rs (L1588-1591)
```rust
    /// Change active status, if set false discard any received messages
    pub fn set_active(&self, active: bool) {
        self.network_state.active.store(active, Ordering::Release);
    }
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
