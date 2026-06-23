### Title
Hardcoded `CorsLayer::permissive()` on RPC HTTP/WS Server Allows Any Web Origin to Call All RPC Methods — (File: `rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC HTTP and WebSocket server unconditionally applies `CorsLayer::permissive()` from `tower-http`, which sets `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: *`, and `Access-Control-Allow-Headers: *` with no configuration option to restrict allowed origins. Any web page loaded in a browser on the same machine as a running CKB node can make cross-origin requests to the RPC endpoint and read the full response, enabling unauthorized invocation of every enabled RPC method including `send_transaction`, `clear_tx_pool`, `remove_transaction`, `set_ban`, and `add_node`/`remove_node`.

---

### Finding Description

In `rpc/src/server.rs`, the `start_server` function builds the axum `Router` and unconditionally layers it with `CorsLayer::permissive()`:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← hardcoded, no config
    ...
``` [1](#0-0) 

`CorsLayer::permissive()` from `tower-http` responds to every CORS preflight with:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, HEAD, OPTIONS, PATCH
Access-Control-Allow-Headers: *
Access-Control-Max-Age: 86400
```

This applies to both the HTTP server (always started) and the optional WebSocket server (same `start_server` code path). The `RpcConfig` struct has no field for allowed origins, so there is no operator-level mitigation: [2](#0-1) 

The default `listen_address` is `127.0.0.1:8114`, which is reachable from any browser tab on the local machine: [3](#0-2) 

---

### Impact Explanation

A malicious web page visited by a node operator can silently call any enabled RPC method and read the full JSON response, because the browser's same-origin policy is fully bypassed by the wildcard CORS response. Concretely exploitable methods include:

- **`send_transaction`** — submit attacker-crafted transactions into the node's mempool and broadcast them to the network.
- **`clear_tx_pool`** / **`remove_transaction`** — evict pending transactions from the local pool, disrupting the operator's own pending activity.
- **`set_ban`** / **`add_node`** / **`remove_node`** — manipulate the node's peer connections, isolating it or forcing connections to attacker-controlled peers.
- **`get_block_template`** / **`submit_block`** — if the Miner module is enabled, read the current mining template or submit a crafted block.
- **`send_alert`** — inject network alerts. [4](#0-3) 

---

### Likelihood Explanation

The attack requires only that the node operator visit any attacker-controlled web page while the node is running — a realistic and common scenario. The default RPC address `127.0.0.1:8114` is reachable from every browser tab on the local machine. No special privileges, leaked keys, or network access beyond the local machine are required. The CORS policy is hardcoded and cannot be restricted through configuration.

---

### Recommendation

1. **Replace `CorsLayer::permissive()` with a restrictive policy.** At minimum, restrict to `null` origin (for direct local tool use) or require an explicit operator-configured allowlist. Example:
   ```rust
   CorsLayer::new()
       .allow_origin(tower_http::cors::AllowOrigin::list([/* operator-configured */]))
       .allow_methods([axum::http::Method::POST, axum::http::Method::OPTIONS])
       .allow_headers(tower_http::cors::AllowHeaders::mirror_request())
   ```

2. **Add a `cors_allowed_origins` field to `RpcConfig`** so operators can explicitly enumerate trusted origins (e.g., a local wallet UI served from a known port). Default should be an empty list (deny all cross-origin).

3. **Document the risk** in the RPC README alongside the existing warning about `listen_address`, noting that even a localhost-bound RPC is reachable from browser tabs on the same machine.

---

### Proof of Concept

With a CKB node running at `127.0.0.1:8114`, the following script executed from any web page origin demonstrates full cross-origin RPC access:

```javascript
// Clears the entire tx pool from a malicious web page
fetch("http://127.0.0.1:8114", {
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
.then(data => console.log("tx pool cleared:", data));
```

The browser sends an OPTIONS preflight; the CKB server responds with `Access-Control-Allow-Origin: *` (from `CorsLayer::permissive()`), the browser permits the follow-up POST, and the response body is fully readable by the attacker's script. The same technique applies to `send_transaction`, `set_ban`, `add_node`, and all other enabled modules. [5](#0-4)

### Citations

**File:** rpc/src/server.rs (L97-154)
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

        let (tx_addr, rx_addr) = tokio::sync::oneshot::channel::<SocketAddr>();

        handler.spawn(async move {
            let listener = tokio::net::TcpListener::bind(
                &address
                    .to_socket_addrs()
                    .expect("config listen_address parsed")
                    .next()
                    .expect("config listen_address parsed"),
            )
            .await
            .unwrap();
            let server = axum::serve(listener, app.into_make_service());

            let _ = tx_addr.send(server.local_addr().unwrap());
            let graceful = server.with_graceful_shutdown(async move {
                new_tokio_exit_rx().cancelled().await;
            });
            drop(graceful.await);
        });

        let rx_addr = handler.block_on(rx_addr)?;
        Ok(rx_addr)
    }
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

**File:** rpc/src/module/pool.rs (L606-692)
```rust
impl PoolRpc for PoolRpcImpl {
    fn tx_pool_ready(&self) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();
        Ok(tx_pool.service_started())
    }

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

    fn test_tx_pool_accept(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<EntryCompleted> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();

        let test_accept_tx_reslt = tx_pool.test_accept_tx(tx).map_err(|e| {
            error!("Send test_tx_pool_accept_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })?;

        test_accept_tx_reslt
            .map(|test_accept_result| test_accept_result.into())
            .map_err(|reject| {
                error!("Send test_tx_pool_accept_tx request error {}", reject);
                RPCError::from_submit_transaction_reject(&reject)
            })
    }

    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }

    fn tx_pool_info(&self) -> Result<TxPoolInfo> {
        let tx_pool = self.shared.tx_pool_controller();
        let get_tx_pool_info = tx_pool.get_tx_pool_info();
        if let Err(e) = get_tx_pool_info {
            error!("Send get_tx_pool_info request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        };

        let tx_pool_info = get_tx_pool_info.unwrap();

        Ok(tx_pool_info.into())
    }

    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```
