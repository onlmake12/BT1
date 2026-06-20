### Title
Missing Per-Method Authorization on Privileged RPC Operations — (`rpc/src/module/pool.rs`, `rpc/src/module/net.rs`)

### Summary
The CKB JSON-RPC server has no authentication or per-method authorization layer. Privileged, destructive operations — `clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`, `set_network_active`, `clear_banned_addresses`, and `set_ban` — are callable by any HTTP client that can reach the RPC endpoint. No credential, token, or caller-identity check exists at the method level or at the server level. Any RPC caller (the explicitly listed attacker profile) can wipe the entire transaction pool, disable all P2P networking, or remove individual transactions without restriction.

### Finding Description

**Root cause — no authentication in the RPC server:**

`rpc/src/server.rs` constructs a plain axum HTTP/TCP/WebSocket server with no authentication middleware: [1](#0-0) 

There is no token check, no HTTP Basic Auth enforcement, and no IP allowlist enforced at the code level. The only protection is the default `listen_address = "127.0.0.1:8114"` in the config file, which operators routinely change for dApp integration.

**Vulnerable privileged methods — `pool.rs`:**

`clear_tx_pool` atomically wipes every pending and proposed transaction from the pool: [2](#0-1) 

`clear_tx_verify_queue` empties the verification queue: [3](#0-2) 

`remove_transaction` removes any specific transaction by hash: [4](#0-3) 

None of these functions perform any caller-identity check before mutating state.

**Vulnerable privileged methods — `net.rs`:**

`set_network_active` can disable all P2P message processing with a single boolean: [5](#0-4) 

`clear_banned_addresses` removes every IP ban entry: [6](#0-5) 

`set_ban` inserts or deletes arbitrary IP/subnet bans: [7](#0-6) 

The underlying `NetworkController::set_active` directly stores the boolean with no guard: [8](#0-7) 

**Contrast with the miner client:** The miner client *sends* HTTP Basic Auth credentials to the node when a URL with embedded credentials is configured: [9](#0-8) 

But the RPC *server* never validates any `Authorization` header — it simply ignores it. There is no server-side enforcement.

### Impact Explanation

| Method | Impact |
|---|---|
| `clear_tx_pool` | Wipes all pending/proposed transactions; users' submitted transactions are silently lost and must be rebroadcast |
| `clear_tx_verify_queue` | Drops all transactions awaiting verification; same loss |
| `remove_transaction(hash)` | Targeted removal of any specific transaction; attacker can selectively censor transactions |
| `set_network_active(false)` | Completely disables P2P message processing; node stops syncing, stops relaying, stops receiving blocks — effective network isolation |
| `clear_banned_addresses` | Removes all IP bans; previously banned malicious peers can immediately reconnect |
| `set_ban` | Attacker can ban legitimate peers (including the node's own upstream) or unban malicious ones |

The most severe combination is `clear_tx_pool` + `set_network_active(false)`: the node's mempool is wiped and it is isolated from the network simultaneously, causing complete service disruption without any on-chain trace.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, but:

1. The config explicitly supports binding to any address via `listen_address`: [10](#0-9) 

2. Many node operators expose the RPC for dApp integration (wallets, indexers, exchanges). The documentation warns against it but provides no code-level enforcement.

3. TCP and WebSocket listeners are also supported and have the same zero-auth surface: [11](#0-10) 

4. Any process on the same host (e.g., a compromised dependency, a malicious npm package in a co-located dApp) can reach `127.0.0.1:8114` even in the default configuration.

An attacker who can send a single HTTP POST to the RPC endpoint — either because the operator exposed it, or via local code execution — can trigger any of the above impacts with zero credentials.

### Recommendation

1. **Add an optional authentication token** to the RPC server. When configured, the server should reject any request that does not present the correct `Authorization: Bearer <token>` or `Authorization: Basic <b64>` header. The miner client already has the client-side infrastructure for Basic Auth; the server side is missing.

2. **Classify methods by privilege level** and enforce the check per-method or per-module. Read-only methods (`get_block`, `tx_pool_info`, etc.) can remain open; destructive methods (`clear_tx_pool`, `set_network_active`, `remove_transaction`, `clear_banned_addresses`, `set_ban`) should require the token.

3. **Add an IP allowlist** at the server level (not just via OS firewall) so that even if the token is absent, only configured source IPs can call privileged methods.

### Proof of Concept

With the RPC exposed (or from localhost):

```bash
# Wipe the entire transaction pool — no credentials required
curl -s -X POST http://<node-rpc>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}

# Disable all P2P networking — node stops syncing immediately
curl -s -X POST http://<node-rpc>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":2}'
# Returns: {"jsonrpc":"2.0","result":null,"id":2}

# Remove a specific high-value pending transaction by hash
curl -s -X POST http://<node-rpc>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xabc..."],"id":3}'
# Returns: {"jsonrpc":"2.0","result":true,"id":3}
```

All three calls succeed with no authentication, no token, and no error. The node's mempool is empty and its P2P layer is silenced.

### Citations

**File:** rpc/src/server.rs (L52-68)
```rust
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }

        let rpc = Arc::new(io_handler);

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
```

**File:** rpc/src/server.rs (L70-88)
```rust
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

**File:** rpc/src/module/net.rs (L686-689)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }
```

**File:** rpc/src/module/net.rs (L691-727)
```rust
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

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```

**File:** network/src/network.rs (L1588-1591)
```rust
    /// Change active status, if set false discard any received messages
    pub fn set_active(&self, active: bool) {
        self.network_state.active.store(active, Ordering::Release);
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
