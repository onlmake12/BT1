### Title
Unrestricted Privileged RPC Operations Allow Any Caller to Disrupt Node State — (`rpc/src/module/pool.rs`, `rpc/src/module/net.rs`)

### Summary
The CKB JSON-RPC server implements no authentication or authorization layer. Privileged, state-mutating operations — specifically `clear_tx_pool()`, `remove_transaction()`, and `set_network_active()` — are callable by any process that can reach the RPC port. There is no `onlyOwner`-equivalent guard anywhere in the handler chain. Any local process (or any remote caller if the operator exposes the port) can silently wipe the entire mempool or disable all P2P networking.

### Finding Description

**Root cause — no authentication middleware in the RPC server:**

`rpc/src/server.rs` builds the axum router with `CorsLayer::permissive()` and a `TimeoutLayer`, but no authentication middleware of any kind:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())          // ← permissive CORS, no auth
    .layer(TimeoutLayer::with_status_code(…))
    .layer(Extension(stream_config));
``` [1](#0-0) 

Every registered RPC method is reachable by any HTTP POST to the listening address with zero credential check.

**Privileged handler 1 — `clear_tx_pool`:**

`rpc/src/module/pool.rs` line 684 directly clears the entire mempool with no caller identity check:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [2](#0-1) 

**Privileged handler 2 — `set_network_active`:**

`rpc/src/module/net.rs` line 772 disables or re-enables all P2P networking with no caller identity check:

```rust
fn set_network_active(&self, state: bool) -> Result<()> {
    self.network_controller.set_active(state);
    Ok(())
}
``` [3](#0-2) 

**Privileged handler 3 — `remove_transaction`:**

`rpc/src/module/pool.rs` line 662 removes any specific transaction (and all its dependents) from the pool with no caller identity check:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [4](#0-3) 

The RPC trait declarations confirm these are production-registered methods in the `Pool` and `Net` modules, both enabled by default: [5](#0-4) [6](#0-5) 

The default configuration binds to `127.0.0.1:8114` with the `Pool` and `Net` modules enabled: [7](#0-6) 

### Impact Explanation

- **`clear_tx_pool`**: Any caller wipes every pending transaction from the mempool in a single call. All user transactions awaiting confirmation are silently discarded. The miner loses all queued fee revenue. This can be repeated continuously to prevent any transaction from ever being included in a block — a targeted, persistent transaction censorship attack.
- **`set_network_active(false)`**: Any caller completely isolates the node from the P2P network. The node stops receiving new blocks and transactions, stops relaying, and stops syncing. The node operator has no indication from the RPC layer that this was done by an unauthorized caller.
- **`remove_transaction`**: Any caller can surgically evict a specific high-fee transaction (e.g., a competitor's transaction) from the pool, enabling fee-based MEV manipulation.

### Likelihood Explanation

The RPC port defaults to localhost, so the immediate attacker surface is any process running on the same machine as the node — malicious software, a compromised dApp backend, or a co-located service. Many production deployments expose the RPC port to a broader network (for wallets, dApps, block explorers) without a reverse proxy enforcing authentication, as the documentation only warns against this rather than enforcing any control. The attack requires a single unauthenticated HTTP POST with a trivial JSON body — no cryptographic material, no PoW, no special protocol knowledge.

### Recommendation

Add an authentication layer to the RPC server. The minimal fix is HTTP Basic Auth or a bearer token checked in an axum middleware before any handler is reached. The `miner/src/client.rs` already implements `parse_authorization` for the outbound miner→node connection, demonstrating the project is aware of the pattern: [8](#0-7) 

The server side should enforce the same credential on inbound requests. Additionally, destructive administrative methods (`clear_tx_pool`, `set_network_active`, `remove_transaction`, `clear_banned_addresses`, `set_ban`) should be separated into a distinct privileged module that is disabled by default and requires explicit opt-in with authentication.

### Proof of Concept

With a default CKB node running (`rpc.listen_address = "127.0.0.1:8114"`, `modules` includes `"Pool"` and `"Net"`):

**Wipe the entire mempool:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"id":1,"jsonrpc":"2.0","result":null}
# All pending transactions are now gone.
```

**Disable all P2P networking:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"set_network_active","params":[false]}'
# Returns: {"id":1,"jsonrpc":"2.0","result":null}
# Node is now isolated from the network.
```

No credentials, no keys, no special access required. Any process on the host (or any remote caller if the port is exposed) can execute these calls.

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

**File:** rpc/src/module/net.rs (L419-420)
```rust
    #[rpc(name = "set_network_active")]
    fn set_network_active(&self, state: bool) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```

**File:** resource/ckb.toml (L182-193)
```text
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
