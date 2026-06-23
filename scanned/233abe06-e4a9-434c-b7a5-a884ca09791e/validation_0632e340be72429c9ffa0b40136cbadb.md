### Title
Unauthenticated Privileged RPC Methods Exposed via Permissive CORS Enable Cross-Origin State Manipulation — (File: `rpc/src/server.rs`)

---

### Summary

The CKB RPC server mounts `CorsLayer::permissive()` with no authentication layer, meaning any web page loaded in a browser on the same machine can issue cross-origin JSON-RPC calls to the local node. Privileged, state-mutating methods — `clear_tx_pool`, `remove_transaction`, `set_ban`, `clear_banned_addresses`, `set_network_active`, `add_node`, `remove_node` — carry no caller-identity check of any kind. An unprivileged attacker who can lure a node operator to visit a malicious URL can silently drain the tx-pool, censor specific transactions, or isolate the node from the network.

---

### Finding Description

**Root cause — permissive CORS with no authentication**

`rpc/src/server.rs` builds the Axum router with:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← allows every origin
    ...
``` [1](#0-0) 

`CorsLayer::permissive()` responds to every CORS preflight with `Access-Control-Allow-Origin: *` and approves all methods and headers. The actual request handler `handle_jsonrpc` performs zero authentication:

```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    // no Authorization header check, no token, no IP allowlist
    ...
    let result = io.handle_call(call, T::default()).await;
``` [2](#0-1) 

**Privileged methods with no caller check**

The Pool module exposes `clear_tx_pool` and `remove_transaction` with no guard:

```rust
#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;

#[rpc(name = "clear_tx_verify_queue")]
fn clear_tx_verify_queue(&self) -> Result<()>;
``` [3](#0-2) 

The Net module exposes `set_ban`, `clear_banned_addresses`, `set_network_active`, `add_node`, `remove_node` — all without any caller-identity verification. [4](#0-3) 

**Miner client sends auth headers the server never validates**

`miner/src/client.rs` implements `parse_authorization` to attach HTTP Basic Auth headers when the configured RPC URL contains credentials:

```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 { ... Some(header) } else { None }
}
``` [5](#0-4) 

The server never reads or validates the `Authorization` header, so this credential mechanism is entirely unenforced — a direct analog to the original report's "authority incorrectly set" finding.

**Attack flow**

1. Node operator runs `ckb run` with default config (`listen_address = "127.0.0.1:8114"`).
2. Operator visits a malicious web page in any browser on the same machine.
3. The page issues a cross-origin `POST` to `http://127.0.0.1:8114` with `Content-Type: application/json`.
4. Browser sends a CORS preflight; `CorsLayer::permissive()` approves it unconditionally.
5. Browser sends the actual JSON-RPC call — e.g., `{"method":"clear_tx_pool","params":[],"id":1,"jsonrpc":"2.0"}`.
6. Server dispatches it with no auth check; tx-pool is wiped.

---

### Impact Explanation

| Method | Impact |
|---|---|
| `clear_tx_pool` | Wipes all pending transactions; node must re-receive them from peers; targeted transactions may never return if peers have already evicted them |
| `remove_transaction` | Silently censors a specific transaction the attacker knows about |
| `set_ban` / `clear_banned_addresses` | Manipulates the peer ban list; can isolate the node or unban malicious peers |
| `set_network_active(false)` | Completely disables P2P networking; node goes offline |
| `add_node` / `remove_node` | Forces connections to attacker-controlled peers or severs legitimate ones |

The impact is **unauthorized state change and service disruption** — matching the original report's "unauthorized modification of farm emissions" class.

---

### Likelihood Explanation

- Default config binds to `127.0.0.1:8114`, reachable from any browser tab on the same machine.
- `CorsLayer::permissive()` is unconditional — no configuration disables it.
- No user interaction beyond visiting a URL is required; the attack is fully scriptable in JavaScript.
- Node operators routinely browse the web while running a node.
- The attacker needs only knowledge of the default port (publicly documented).

---

### Recommendation

1. **Remove `CorsLayer::permissive()`** or replace it with an explicit allowlist of trusted origins (e.g., `localhost` only). [6](#0-5) 
2. **Add an optional HTTP Basic Auth middleware** to the Axum router that validates the `Authorization` header against a node-operator-configured secret, making the miner client's existing `parse_authorization` logic actually enforceable. [5](#0-4) 
3. **Separate privileged methods** (`clear_tx_pool`, `set_ban`, `set_network_active`, etc.) from read-only methods and gate them behind the auth middleware independently of the module-enable flags. [3](#0-2) 

---

### Proof of Concept

A malicious web page served from any origin:

```html
<script>
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    id: 1, jsonrpc: "2.0",
    method: "clear_tx_pool", params: []
  })
});
</script>
```

**Expected outcome:** Browser sends CORS preflight to `127.0.0.1:8114`; `CorsLayer::permissive()` approves it; browser sends the POST; `handle_jsonrpc` dispatches `clear_tx_pool` with no auth check; all pending transactions are removed from the pool; the RPC returns `{"result":null}`. The node operator sees no error and has no indication the pool was cleared.

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

**File:** rpc/src/module/pool.rs (L322-350)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;

    /// Removes all transactions from the verification queue.
    ///
    /// ## Examples
    ///
    /// Request
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "method": "clear_tx_verify_queue",
    ///   "params": []
    /// }
    /// ```
    ///
    /// Response
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "result": null
    /// }
    /// ```
    #[rpc(name = "clear_tx_verify_queue")]
    fn clear_tx_verify_queue(&self) -> Result<()>;
```

**File:** rpc/README.md (L96-105)
```markdown
        * [Method `local_node_info`](#net-local_node_info)
        * [Method `get_peers`](#net-get_peers)
        * [Method `get_banned_addresses`](#net-get_banned_addresses)
        * [Method `clear_banned_addresses`](#net-clear_banned_addresses)
        * [Method `set_ban`](#net-set_ban)
        * [Method `sync_state`](#net-sync_state)
        * [Method `set_network_active`](#net-set_network_active)
        * [Method `add_node`](#net-add_node)
        * [Method `remove_node`](#net-remove_node)
        * [Method `ping_peers`](#net-ping_peers)
```

**File:** miner/src/client.rs (L380-393)
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
```
