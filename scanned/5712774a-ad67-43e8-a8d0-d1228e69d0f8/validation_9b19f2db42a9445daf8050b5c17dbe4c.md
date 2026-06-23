### Title
Permissive CORS Policy Combined with No RPC Authentication Allows Any Website to Invoke Privileged State-Mutating Methods Against a Running Node - (File: `rpc/src/server.rs`)

---

### Summary

The CKB RPC server is configured with `CorsLayer::permissive()`, which instructs browsers to allow cross-origin requests from any origin. Because no authentication or CSRF token is required on any RPC method, a malicious web page visited by a node operator can silently invoke privileged, state-mutating RPC methods — including `clear_tx_pool`, `set_network_active`, and `set_ban` — that are enabled by default in the production module list. This is a direct analog to the MSQ finding: privileged administrative actions are reachable without any consent or credential check from an untrusted origin.

---

### Finding Description

**Root cause — permissive CORS with no authentication:**

In `rpc/src/server.rs`, the HTTP server is built with:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    ...
    .layer(CorsLayer::permissive())   // ← allows all origins
    ...
``` [1](#0-0) 

`CorsLayer::permissive()` causes the server to respond with `Access-Control-Allow-Origin: *` (and permissive `Access-Control-Allow-Methods`, `Access-Control-Allow-Headers`). This instructs browsers to allow cross-origin `fetch`/`XMLHttpRequest` calls to the RPC endpoint from any web page. There is no authentication middleware, no CSRF token, no API key, and no per-method caller identity check anywhere in the server or handler chain.

**Privileged methods enabled by default in production:**

The production configuration (`resource/ckb.toml`) enables the `Net` and `Pool` modules by default:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [2](#0-1) 

These modules expose the following privileged, state-mutating methods with zero authorization checks:

- `set_network_active(false)` — completely disables all P2P networking on the node
- `clear_tx_pool` — destroys every pending transaction in the mempool
- `clear_tx_verify_queue` — clears the verification queue
- `set_ban` — bans arbitrary peer IP addresses
- `remove_transaction` — evicts a specific transaction from the pool [3](#0-2) [4](#0-3) [5](#0-4) 

None of these handlers perform any credential check. For example, `set_network_active` in `NetRpcImpl` directly calls `self.network_controller.set_network_active(state)` with no guard: [6](#0-5) 

Similarly, `clear_tx_pool` in `PoolRpcImpl` directly invokes the tx-pool controller with no authorization: [7](#0-6) 

The `RpcConfig` struct has no authentication fields — no token, no allowed-origin list, no credential store: [8](#0-7) 

---

### Impact Explanation

An attacker who controls a web page visited by the node operator (or any user on the same machine as the node) can silently issue cross-origin POST requests to `http://127.0.0.1:8114` and invoke any enabled RPC method. Because `CorsLayer::permissive()` is set, the browser will not block the preflight or the actual request.

Concrete impacts reachable without any privileged access:

1. **Network isolation**: `set_network_active(false)` stops all P2P activity. The node stops syncing, stops relaying transactions and blocks, and becomes invisible to the network — without the operator knowing.
2. **Mempool destruction**: `clear_tx_pool` evicts all pending transactions. For a mining pool or exchange node, this causes loss of fee revenue and disrupts user transaction processing.
3. **Peer manipulation**: `set_ban` can ban all known good peers, isolating the node from honest peers and making it susceptible to eclipse attacks.
4. **Transaction censorship**: `remove_transaction` can selectively evict specific transactions from the pool.

---

### Likelihood Explanation

The attack requires the node operator (or any local user) to visit a malicious web page while the CKB node is running. This is a realistic scenario for:

- Node operators who run a full node on a desktop or workstation and browse the web normally.
- Shared hosting or cloud environments where a co-tenant can serve a web page to the operator.
- Any SSRF vulnerability in a co-hosted web application that can make server-side requests to `127.0.0.1:8114`.

The RPC default bind address is `127.0.0.1:8114`, which is reachable from the local browser. The `CorsLayer::permissive()` setting is unconditional — it is not gated on any configuration flag.

---

### Recommendation

1. Remove `CorsLayer::permissive()` or replace it with an explicit allowlist of trusted origins (e.g., only `null` for local file pages, or a specific configured origin).
2. Add an optional authentication token to `RpcConfig` and enforce it as a middleware layer in `start_server`, checked before dispatching any method.
3. Separate destructive/state-mutating methods (`clear_tx_pool`, `set_network_active`, `set_ban`) into a distinct module that requires explicit opt-in and authentication, analogous to the MSQ recommendation to always require consent for critical actions.

---

### Proof of Concept

A malicious web page served from any origin can execute the following JavaScript while the node operator has the page open:

```javascript
// Silently disable all P2P networking on the victim's CKB node
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    jsonrpc: "2.0",
    method: "set_network_active",
    params: [false],
    id: 1
  })
});

// Simultaneously destroy the entire mempool
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    jsonrpc: "2.0",
    method: "clear_tx_pool",
    params: [],
    id: 2
  })
});
```

Because `CorsLayer::permissive()` causes the server to respond with `Access-Control-Allow-Origin: *`, the browser will complete both requests without any CORS block. No credentials, tokens, or privileged access are required. The node's P2P layer is silenced and its mempool is emptied without any visible indication to the operator.

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/module/net.rs (L419-420)
```rust
    #[rpc(name = "set_network_active")]
    fn set_network_active(&self, state: bool) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L547-552)
```rust
#[derive(Clone)]
pub(crate) struct NetRpcImpl {
    pub network_controller: NetworkController,
    pub sync_shared: Arc<SyncShared>,
    pub chain_controller: Arc<ChainController>,
}
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

**File:** rpc/src/module/pool.rs (L471-527)
```rust
#[derive(Clone)]
pub(crate) struct PoolRpcImpl {
    shared: Shared,
    well_known_lock_scripts: Vec<packed::Script>,
    well_known_type_scripts: Vec<packed::Script>,
}

impl PoolRpcImpl {
    pub fn new(
        shared: Shared,
        mut extra_well_known_lock_scripts: Vec<packed::Script>,
        mut extra_well_known_type_scripts: Vec<packed::Script>,
    ) -> PoolRpcImpl {
        let mut well_known_lock_scripts =
            build_well_known_lock_scripts(shared.consensus().id.as_str());
        let mut well_known_type_scripts =
            build_well_known_type_scripts(shared.consensus().id.as_str());

        well_known_lock_scripts.append(&mut extra_well_known_lock_scripts);
        well_known_type_scripts.append(&mut extra_well_known_type_scripts);

        PoolRpcImpl {
            shared,
            well_known_lock_scripts,
            well_known_type_scripts,
        }
    }

    fn check_output_validator(
        &self,
        outputs_validator: Option<OutputsValidator>,
        tx: &TransactionView,
    ) -> Result<()> {
        if let Err(e) = match outputs_validator {
            None | Some(OutputsValidator::Passthrough) => Ok(()),
            Some(OutputsValidator::WellKnownScriptsOnly) => WellKnownScriptsOnlyValidator::new(
                self.shared.consensus(),
                &self.well_known_lock_scripts,
                &self.well_known_type_scripts,
            )
            .validate(tx),
        } {
            return Err(RPCError::custom_with_data(
                RPCError::PoolRejectedTransactionByOutputsValidator,
                format!(
                    "The transaction is rejected by OutputsValidator set in params[1]: {}. \
                    Please check the related information in https://github.com/nervosnetwork/ckb/wiki/Transaction-%C2%BB-Default-Outputs-Validator",
                    outputs_validator
                        .unwrap_or(OutputsValidator::WellKnownScriptsOnly)
                        .json_display()
                ),
                e,
            ));
        }
        Ok(())
    }
}
```

**File:** util/app-config/src/configs/rpc.rs (L24-61)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```
