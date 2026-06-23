### Title
Unauthenticated RPC Callers Can Execute Privileged Node Management Operations Without Any Authorization Check - (File: rpc/src/server.rs, rpc/src/module/pool.rs, rpc/src/module/net.rs)

### Summary
The CKB JSON-RPC server exposes privileged, destructive operations — including `clear_tx_pool`, `remove_transaction`, `set_ban`, and `clear_banned_addresses` — to any caller who can reach the RPC port, with zero authentication or per-method authorization enforcement. The `handle_jsonrpc` dispatcher in `rpc/src/server.rs` routes every incoming request directly to the method handler with no identity check, no token validation, and no role gate. Any local process or remote client (if the operator has exposed the port) can wipe the entire mempool, surgically remove specific transactions, or manipulate the peer ban list without supplying any credential.

### Finding Description

**Root cause — no authentication or authorization layer in the RPC server:**

`rpc/src/server.rs` builds the Axum router with `CorsLayer::permissive()` and no authentication middleware. Every POST to `/` is forwarded directly to `handle_jsonrpc`, which calls `io.handle_call(call, T::default())` with a default (empty) metadata value. There is no token check, no session check, and no caller-identity propagation at any point in the dispatch path. [1](#0-0) [2](#0-1) 

**Privileged operations reachable with zero credentials:**

`PoolRpc::clear_tx_pool` (Pool module) atomically removes every transaction from the mempool. Its implementation in `PoolRpcImpl` performs no caller check before calling `tx_pool.clear_pool(snapshot)`: [3](#0-2) 

`PoolRpc::remove_transaction` lets any caller remove an arbitrary specific transaction and all its descendants: [4](#0-3) 

`NetRpc::set_ban` lets any caller insert or delete IP/subnet bans, and `clear_banned_addresses` lets any caller wipe the entire ban list — both with no authorization gate: [5](#0-4) 

**No per-module or per-method permission model exists.** The only access control is the OS-level network binding (`listen_address = "127.0.0.1:8114"` by default). Once a caller can reach the port — whether via local process, SSRF, or operator misconfiguration — every method in every enabled module is equally accessible. [6](#0-5) 

The `Debug` module (`update_main_logger`, `set_extra_logger`, `jemalloc_profiling_dump`) is similarly unguarded: [7](#0-6) 

### Impact Explanation

**`clear_tx_pool`**: Wipes every pending and proposed transaction from the node's mempool in a single unauthenticated call. Users who submitted fee-paying transactions lose them; the node operator's block-assembly pipeline is reset. Repeated calls keep the pool empty, preventing any transaction from accumulating enough confirmations to be mined.

**`remove_transaction`**: Allows targeted, surgical removal of a specific transaction and all its descendants. An attacker who knows a victim's transaction hash (observable via `get_raw_tx_pool` or the P2P relay network) can evict it before it is mined, forcing the victim to resubmit and potentially pay higher fees or miss time-sensitive windows.

**`set_ban` / `clear_banned_addresses`**: An attacker can ban all legitimate peers (isolating the node) or unban previously-banned malicious peers (re-exposing the node to eclipse or spam attacks).

**`update_main_logger` / `set_extra_logger`**: An attacker can redirect or suppress logs, hindering incident detection.

Impact: 5 — destructive, irreversible within the current session, directly affects transaction processing and network connectivity.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, so exploitation requires the ability to make HTTP requests to that address. This is reachable by:
- Any local process on the same host (the "supported local CLI/RPC user" attacker profile explicitly listed in scope).
- Any web page open in a browser on the same host (via `fetch`/`XMLHttpRequest` to localhost — `CorsLayer::permissive()` removes the CORS barrier).
- Any SSRF vulnerability in a co-located service.
- Any operator who has bound the RPC to `0.0.0.0` for remote management (a common real-world configuration).

Likelihood: 4 — the localhost binding is a meaningful barrier, but the CORS-permissive browser-reachable surface and the "local RPC user" attacker profile make exploitation realistic without any privileged access.

### Recommendation

Introduce a per-method authorization tier. At minimum:
1. Add an optional bearer-token or HTTP Basic auth middleware in `rpc/src/server.rs` that gates all state-mutating methods.
2. Classify RPC methods into read-only vs. privileged-write tiers and enforce the distinction at the dispatcher level, independent of which module is enabled.
3. Remove `CorsLayer::permissive()` or replace it with an explicit allowlist to prevent browser-based SSRF exploitation of the localhost port.

### Proof of Concept

With the node running at default configuration (RPC on `127.0.0.1:8114`, Pool module enabled):

```bash
# Wipe the entire mempool — no credentials required
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}

# Remove a specific transaction by hash
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],"id":2}'
# Returns: {"jsonrpc":"2.0","result":true,"id":2}

# Ban a legitimate peer IP
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"set_ban","params":["1.2.3.4","insert",null,null,"attacker"],"id":3}'
# Returns: {"jsonrpc":"2.0","result":null,"id":3}
```

All three calls succeed with no authentication token, no session cookie, and no special header. The `handle_jsonrpc` dispatcher in `rpc/src/server.rs` routes them directly to `PoolRpcImpl::clear_tx_pool`, `PoolRpcImpl::remove_transaction`, and `NetRpcImpl::set_ban` respectively, none of which perform any caller-identity check before executing the privileged operation. [8](#0-7) [9](#0-8) [10](#0-9)

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

**File:** rpc/src/server.rs (L218-259)
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

**File:** rpc/src/module/net.rs (L286-287)
```rust
    #[rpc(name = "clear_banned_addresses")]
    fn clear_banned_addresses(&self) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L686-727)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }

    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()> {
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
            }
            "delete" => {
                self.network_controller.unban(&ip_network);
                Ok(())
            }
            _ => Err(RPCError::invalid_params(format!(
                "Expected `params[1]` to be in the list [insert, delete], got {address}"
            ))),
        }
    }
```

**File:** rpc/src/service_builder.rs (L192-196)
```rust
    /// Mounts methods from module Debug if it is enabled in the config.
    pub fn enable_debug(mut self) -> Self {
        let methods = DebugRpcImpl {};
        set_rpc_module_methods!(self, "Debug", debug_enable, add_debug_rpc_methods, methods)
    }
```

**File:** rpc/src/module/debug.rs (L59-94)
```rust
    fn update_main_logger(&self, config: MainLoggerConfig) -> Result<()> {
        let MainLoggerConfig {
            filter,
            to_stdout,
            to_file,
            color,
        } = config;
        if filter.is_none() && to_stdout.is_none() && to_file.is_none() && color.is_none() {
            return Ok(());
        }
        Logger::update_main_logger(filter, to_stdout, to_file, color).map_err(|err| Error {
            code: InternalError,
            message: err,
            data: None,
        })
    }

    fn set_extra_logger(&self, name: String, config_opt: Option<ExtraLoggerConfig>) -> Result<()> {
        if let Err(err) = Logger::check_extra_logger_name(&name) {
            return Err(Error {
                code: InternalError,
                message: err,
                data: None,
            });
        }
        if let Some(config) = config_opt {
            Logger::update_extra_logger(name, config.filter)
        } else {
            Logger::remove_extra_logger(name)
        }
        .map_err(|err| Error {
            code: InternalError,
            message: err,
            data: None,
        })
    }
```
