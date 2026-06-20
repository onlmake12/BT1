### Title
Hardcoded `CorsLayer::permissive()` Enables Cross-Origin RPC Exploitation Against Any Node Operator's Browser — (`rpc/src/server.rs`)

---

### Summary

The CKB RPC HTTP server unconditionally applies `CorsLayer::permissive()` to every route. This hardcoded policy sets `Access-Control-Allow-Origin: *` and allows all methods and headers from any origin. Because the RPC server has no authentication, any malicious webpage visited by a node operator can make cross-origin `fetch` POST requests to `http://127.0.0.1:8114/` and invoke any enabled RPC method — including `clear_tx_pool`, `set_ban`, `set_network_active`, `send_transaction`, and `remove_transaction` — without the operator's knowledge or consent.

---

### Finding Description

In `rpc/src/server.rs`, the `start_server` function builds the axum `Router` and unconditionally layers it with `CorsLayer::permissive()`:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← hardcoded, no configuration knob
    ...
``` [1](#0-0) 

`tower_http::cors::CorsLayer::permissive()` responds to every preflight `OPTIONS` request with:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: *
Access-Control-Allow-Headers: *
```

This satisfies the browser's CORS preflight check for `Content-Type: application/json` POST requests. After the preflight succeeds, the browser sends the actual JSON-RPC POST body and delivers the response back to the attacker's script.

There is no IP allowlist, no authentication token, and no `Origin` header validation anywhere in the RPC server code. The `RpcConfig` struct has no field to restrict allowed origins: [2](#0-1) 

The default `listen_address` is `127.0.0.1:8114`, which is localhost-only: [3](#0-2) 

However, the CORS policy is what makes the localhost binding insufficient as a security boundary: browsers enforce the same-origin policy on responses, but `CorsLayer::permissive()` explicitly opts out of that protection for every cross-origin caller.

---

### Impact Explanation

An attacker who can get a node operator to visit a malicious webpage (e.g., via a phishing link, a compromised ad network, or a malicious DApp frontend) can silently invoke any enabled RPC method. The default enabled modules include `Net`, `Pool`, `Miner`, `Chain`, `Stats`, `Subscription`, `Experiment`, and `Terminal`: [4](#0-3) 

Concrete impacts per module:

- **Pool module** — `clear_tx_pool()` wipes all pending transactions from the mempool, disrupting the operator's pending transactions and any relayed user transactions. [5](#0-4) 

- **Pool module** — `send_transaction()` submits attacker-crafted transactions into the pool, which are then relayed to the network. [6](#0-5) 

- **Net module** — `set_ban("0.0.0.0/0", "insert", ...)` bans all peers, fully isolating the node from the P2P network. [7](#0-6) 

- **Net module** — `clear_banned_addresses()` removes all existing bans, re-exposing the node to previously-banned malicious peers. [8](#0-7) 

- **Miner module** — `get_block_template()` leaks the current mining template (pending transactions, coinbase, etc.) to the attacker's origin. [9](#0-8) 

---

### Likelihood Explanation

The attack requires only that the node operator's browser visits a page controlled by the attacker while the CKB node is running locally. This is a realistic scenario for:

- Node operators who also use a browser on the same machine (the common case for home/hobbyist nodes and developers).
- DApp developers who run a local node and browse DApp frontends that could be compromised.
- Any operator who clicks a link in a chat, email, or forum post.

The attack is silent (no UI prompt), requires no special privileges, and works against the default configuration. The `CorsLayer::permissive()` call is hardcoded — the operator cannot mitigate it through configuration alone.

---

### Recommendation

1. **Replace `CorsLayer::permissive()` with a restrictive policy.** At minimum, restrict to `null` origin (for localhost-only use) or require an explicit allowlist configured by the operator. A safe default for a localhost-bound server is to reject all cross-origin requests:

   ```rust
   .layer(CorsLayer::new()) // deny all cross-origin by default
   ```

2. **Add an `allowed_origins` field to `RpcConfig`** so operators who intentionally expose the RPC to other machines can explicitly enumerate trusted origins.

3. **Consider adding an optional secret token** (e.g., a bearer token or cookie) required on all RPC requests, so that even if CORS is permissive, cross-origin scripts cannot forge valid requests without knowing the token.

---

### Proof of Concept

With a CKB node running at `127.0.0.1:8114`, the following script hosted on any origin (e.g., `https://evil.example.com`) will successfully clear the node's transaction pool:

```html
<script>
fetch("http://127.0.0.1:8114/", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    id: 1,
    jsonrpc: "2.0",
    method: "clear_tx_pool",
    params: []
  })
})
.then(r => r.json())
.then(data => console.log("Result:", data));
// Browser sends OPTIONS preflight → server responds Access-Control-Allow-Origin: *
// Browser sends POST → server executes clear_tx_pool → mempool wiped
</script>
```

The preflight succeeds because `CorsLayer::permissive()` unconditionally returns `Access-Control-Allow-Origin: *` for all origins. [9](#0-8)

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

**File:** util/app-config/src/configs/rpc.rs (L24-61)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```

**File:** resource/ckb.toml (L177-184)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/module/pool.rs (L612-635)
```rust
    fn send_transaction(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<H256> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();
        let submit_tx = tx_pool.submit_local_tx(tx.clone());

        if let Err(e) = submit_tx {
            error!("Send submit_tx request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        }

        let tx_hash = tx.hash();
        match submit_tx.unwrap() {
            Ok(_) => Ok(tx_hash.into()),
            Err(reject) => Err(RPCError::from_submit_transaction_reject(&reject)),
        }
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

**File:** rpc/src/module/net.rs (L286-287)
```rust
    #[rpc(name = "clear_banned_addresses")]
    fn clear_banned_addresses(&self) -> Result<()>;
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
