### Title
Any RPC Caller Can Wipe the Entire Transaction Pool via Unauthenticated `clear_tx_pool` — (`rpc/src/module/pool.rs`)

---

### Summary

The `Pool` RPC module exposes `clear_tx_pool` and `clear_tx_verify_queue` as public JSON-RPC methods with no per-caller authentication or authorization check. These are highly destructive admin-level operations that remove all pending transactions from the node's mempool and verification queue respectively. Because the CKB RPC server implements no token, session, or role-based access control at the method level, any caller who can reach the RPC endpoint — including any co-located process or any remote caller when the RPC is exposed for mining — can invoke these methods and wipe the entire mempool without restriction.

---

### Finding Description

**Root cause — no access control on privileged state-mutating RPC methods:**

The `PoolRpcImpl::clear_tx_pool` implementation in `rpc/src/module/pool.rs` directly calls `tx_pool.clear_pool(snapshot)` with no caller identity check: [1](#0-0) 

Similarly, `clear_tx_verify_queue` clears the entire verification queue: [2](#0-1) 

And `remove_transaction` allows any caller to evict any specific transaction by hash: [3](#0-2) 

These methods are registered under the `Pool` module, which is **enabled by default** in `ckb.toml`: [4](#0-3) 

The RPC server (`rpc/src/server.rs`) uses axum with no authentication middleware — no bearer token, no HTTP Basic Auth check, no session validation: [5](#0-4) 

The `ServiceBuilder` mounts all Pool methods together with no per-method privilege distinction: [6](#0-5) 

There is no mechanism to enable `send_transaction` (a normal user operation) without simultaneously enabling `clear_tx_pool` (a privileged admin operation), because they share the same module and the same registration path.

**Attacker entry path:**

The RPC binds to `127.0.0.1:8114` by default. However, the `Miner` module is explicitly designed for remote access — the miner client connects to the node's RPC over the network to call `get_block_template` and `submit_block`. Mining pool operators routinely expose the RPC endpoint to their miner fleet. When the RPC is exposed (a supported and documented deployment), all enabled module methods — including `clear_tx_pool` — become reachable by any network caller with no authentication required.

Additionally, any process co-located on the same host (e.g., a compromised dApp, a malicious dependency, or a web server on the same machine) can reach the localhost RPC and call `clear_tx_pool` without any credential.

The `Pool` module is also the module operators must enable to allow users to submit transactions via `send_transaction`. There is no way to enable normal user-facing pool operations without also exposing the destructive admin operations.

---

### Impact Explanation

- **Transaction pool wipe**: A single unauthenticated `clear_tx_pool` call removes every pending transaction from the mempool. All user-submitted transactions are silently dropped and must be resubmitted.
- **Miner revenue disruption**: Miners lose all pending fee-bearing transactions from their block template, directly reducing mining revenue.
- **Selective censorship via `remove_transaction`**: An attacker can call `remove_transaction` with a specific tx hash to silently evict a targeted transaction (e.g., a high-value DeFi settlement or a time-sensitive DAO vote) from the pool, preventing it from being mined.
- **Verification queue disruption**: `clear_tx_verify_queue` drops all transactions currently being validated, stalling the node's processing pipeline.

These are unauthorized state changes with direct financial impact on node operators and their users.

---

### Likelihood Explanation

- The `Pool` module is enabled by default in the production `ckb.toml` configuration.
- Mining pool operators and dApp node operators routinely expose the RPC endpoint to remote callers (miners, relayers, wallets).
- The miner HTTP client explicitly supports remote RPC access with optional Basic Auth on the **client side** only — the server performs no corresponding auth check.
- Any co-located process on a node host can reach `127.0.0.1:8114` without any credential.
- The attack requires a single HTTP POST with a trivial JSON body — no cryptographic capability, no staked asset, no privileged key.

---

### Recommendation

Introduce per-method or per-module access control at the RPC server layer. Specifically:

1. **Split the Pool module** into a public sub-module (`send_transaction`, `test_tx_pool_accept`, `tx_pool_info`, `get_raw_tx_pool`) and an admin sub-module (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`). Allow operators to enable each independently.
2. **Add an authentication token** to the RPC server (e.g., a configurable bearer token or HTTP Basic Auth secret checked server-side) that must be presented to invoke admin-class methods.
3. At minimum, document `clear_tx_pool` and `remove_transaction` as admin-only operations and gate them behind a separate module flag so operators can expose `send_transaction` without exposing the destructive methods.

---

### Proof of Concept

With the `Pool` module enabled (default) and the RPC accessible:

```bash
# Wipe the entire transaction pool — no authentication required
curl -s -X POST http://<node-rpc-address>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'

# Response: {"jsonrpc":"2.0","result":null,"id":1}
# All pending transactions are now gone from the mempool.

# Selectively evict a specific transaction by hash
curl -s -X POST http://<node-rpc-address>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],"id":2}'

# Response: {"jsonrpc":"2.0","result":true,"id":2}
# Target transaction is silently dropped.
```

No credentials, no privileged key, no special role — any caller who can reach the RPC endpoint can execute these operations. The direct analog to the `extendTime` bug is that `clear_tx_pool` is a privileged admin function registered as a plain public RPC method with no `onlyOwner`-equivalent guard.

### Citations

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/server.rs (L1-32)
```rust
use crate::IoHandler;
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Extension, Router};
use axum_streams::StreamBodyAs;
use ckb_app_config::RpcConfig;
use ckb_async_runtime::Handle;
use ckb_error::AnyError;
use ckb_logger::info;

use axum::{Json, body::Bytes, http::StatusCode, response::Response};

use jsonrpc_core::{MetaIoHandler, Metadata, Request};

use ckb_stop_handler::{CancellationToken, new_tokio_exit_rx};
use futures_util::{StreamExt, stream};
use jsonrpc_core::Error;
use jsonrpc_core::types::Response as RpcResponse;
use jsonrpc_core::types::error::ErrorCode;

use futures_util::{SinkExt, TryStreamExt};
use jsonrpc_utils::axum_utils::handle_jsonrpc_ws;
use jsonrpc_utils::pub_sub::Session;
use jsonrpc_utils::stream::{StreamMsg, StreamServerConfig, serve_stream_sink};
use std::net::{SocketAddr, ToSocketAddrs};
use std::sync::Arc;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio_util::codec::{FramedRead, FramedWrite, LinesCodec, LinesCodecError};
use tower_http::cors::CorsLayer;
use tower_http::timeout::TimeoutLayer;
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
