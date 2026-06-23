### Title
Privileged RPC Operations Are Not Caller-Gated — Any Reachable RPC Caller Can Wipe the Mempool or Disable P2P Networking - (File: `rpc/src/module/pool.rs`, `rpc/src/module/net.rs`)

---

### Summary

The CKB RPC server exposes several destructive, admin-level operations — including `clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`, `set_network_active`, `set_ban`, and `clear_banned_addresses` — with **no authentication or authorization check of any kind**. The server is simultaneously configured with `CorsLayer::permissive()`, which allows cross-origin requests from any web origin. Any caller who can reach the RPC endpoint (directly or via CSRF) can invoke these operations unconditionally.

---

### Finding Description

**Root cause — no auth layer in the RPC server:**

`rpc/src/server.rs` builds the Axum router with no authentication middleware and with fully permissive CORS:

```rust
// rpc/src/server.rs lines 119-129
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← any origin allowed
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
```

There is no token check, no IP allowlist middleware, and no per-method access control at the handler level.

**Root cause — privileged pool methods have no caller check:**

`rpc/src/module/pool.rs` implements `clear_tx_pool` and `clear_tx_verify_queue` with zero authorization logic:

```rust
// pool.rs lines 684-701
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

`remove_transaction` is equally unguarded (pool.rs lines 662–669).

**Root cause — privileged net methods have no caller check:**

`rpc/src/module/net.rs` implements `set_network_active`, `set_ban`, and `clear_banned_addresses` with no authorization:

```rust
// net.rs lines 772-774
fn set_network_active(&self, state: bool) -> Result<()> {
    self.network_controller.set_active(state);
    Ok(())
}
```

```rust
// net.rs lines 686-689
fn clear_banned_addresses(&self) -> Result<()> {
    self.network_controller.clear_banned_addrs();
    Ok(())
}
```

**Module registration confirms no gating is added at the service layer:**

`rpc/src/service_builder.rs` registers all modules via `set_rpc_module_methods!`, which only checks whether a module is *enabled* in config — not whether the *caller* is authorized:

```rust
// service_builder.rs lines 36-47
macro_rules! set_rpc_module_methods {
    ($self:ident, $name:expr, $check:ident, $add_methods:ident, $methods:expr) => {{
        ...
        if $self.config.$check() {
            $self.add_methods(meta_io);   // ← enabled = fully public
        } else {
            $self.update_disabled_methods($name, meta_io);
        }
    }};
}
```

---

### Impact Explanation

An attacker who can reach the RPC endpoint can:

1. **Wipe the entire mempool** via `clear_tx_pool` — all pending transactions are discarded, causing economic disruption to users waiting for confirmations and potentially enabling fee-sniping or double-spend windows.
2. **Disable all P2P networking** via `set_network_active(false)` — the node is silently isolated from the network, stopping block and transaction propagation without the operator's knowledge.
3. **Censor specific transactions** via `remove_transaction` — targeted removal of any pending transaction from the pool.
4. **Manipulate the peer ban list** via `set_ban` / `clear_banned_addresses` — an attacker can unban previously-banned malicious peers or ban legitimate peers, disrupting the node's peer management.

---

### Likelihood Explanation

There are two realistic attacker paths:

**Path 1 — RPC exposed to a non-localhost address (common for remote management):**
The `listen_address` in `RpcConfig` is fully configurable. Many node operators bind the RPC to a non-loopback address for remote access. In this configuration, any internet-reachable caller can invoke all of the above methods with a single HTTP POST.

**Path 2 — CSRF against a localhost-bound RPC:**
Even when the RPC is bound to `127.0.0.1`, the server responds with `Access-Control-Allow-Origin: *` (from `CorsLayer::permissive()`). A malicious web page visited by the node operator can issue cross-origin `fetch` POST requests to `http://127.0.0.1:8114` and invoke `clear_tx_pool` or `set_network_active(false)` without any user interaction beyond visiting the page. The permissive CORS header is the server-side enabler of this attack.

---

### Recommendation

1. **Add token-based authentication** for privileged RPC methods. A secret token configured at startup should be required in a request header (e.g., `Authorization: Bearer <token>`) for all state-mutating admin operations.
2. **Replace `CorsLayer::permissive()` with a restrictive allowlist** — at minimum, disallow cross-origin requests to state-mutating methods, or restrict the `Access-Control-Allow-Origin` header to trusted origins.
3. **Separate privileged methods** onto a distinct listen address/port (e.g., an admin socket bound strictly to `127.0.0.1` or a Unix domain socket) so that network-level controls can be applied independently of the public RPC surface.

---

### Proof of Concept

```bash
# Wipe the entire mempool — no credentials required:
curl -s -X POST http://<node-rpc>:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}

# Disable all P2P networking — no credentials required:
curl -s -X POST http://<node-rpc>:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}

# CSRF variant (malicious web page, RPC on localhost):
# <script>
# fetch("http://127.0.0.1:8114", {
#   method: "POST",
#   headers: {"Content-Type": "application/json"},
#   body: JSON.stringify({jsonrpc:"2.0",method:"clear_tx_pool",params:[],id:1})
# });
# </script>
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rpc/src/module/net.rs (L686-689)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }
```

**File:** rpc/src/module/net.rs (L772-774)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
```

**File:** rpc/src/service_builder.rs (L36-47)
```rust
macro_rules! set_rpc_module_methods {
    ($self:ident, $name:expr, $check:ident, $add_methods:ident, $methods:expr) => {{
        let mut meta_io = MetaIoHandler::default();
        $add_methods(&mut meta_io, $methods);
        if $self.config.$check() {
            $self.add_methods(meta_io);
        } else {
            $self.update_disabled_methods($name, meta_io);
        }
        $self
    }};
}
```
