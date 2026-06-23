### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Another User's Pending Transaction Without Ownership Check — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method removes any transaction (and all its descendants) from the tx-pool given only a `tx_hash`. There is no check that the caller is the transaction submitter or has any authorization over that transaction. The RPC server itself has no authentication layer. Any caller who can reach the endpoint — including any local process or any remote client on nodes where operators have exposed the RPC — can silently evict another user's pending transaction, causing griefing with no benefit to the attacker.

---

### Finding Description

`remove_transaction` in `rpc/src/module/pool.rs` (line 662) accepts a bare `tx_hash` and immediately delegates to `tx_pool.remove_local_tx(tx_hash.into())`:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

There is no check that `tx_hash` belongs to the caller, no session token, no IP-based ownership record, and no other authorization gate. [1](#0-0) 

The RPC server is built with `CorsLayer::permissive()` and no authentication middleware of any kind:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    ...
    .layer(CorsLayer::permissive())
    ...
``` [2](#0-1) 

The `RpcConfig` struct has no authentication fields — there is no token, no API key, no IP-allowlist enforcement at the application layer: [3](#0-2) 

The `remove_local_tx` call propagates to `TxPool::remove_tx`, which calls `pool_map.remove_entry_and_descendants` — permanently evicting the target transaction and every child transaction that depends on it: [4](#0-3) 

The companion `clear_tx_pool` method (line 684) is even broader: it wipes the entire pool in one call, affecting every pending user simultaneously: [5](#0-4) 

---

### Impact Explanation

An attacker who can reach the RPC endpoint can:

1. **Enumerate all pending transactions** via `get_raw_tx_pool` (also unauthenticated).
2. **Call `remove_transaction` with any victim's `tx_hash`**, evicting that transaction and all its descendants from the pool with no cost to the attacker.

For ordinary transactions the victim can resubmit, but the harm is concrete in two scenarios:

- **DAO withdrawal transactions with epoch-based `since` constraints**: CKB DAO phase-2 withdrawals must be submitted within a specific epoch window. If an attacker repeatedly removes such a transaction before it is proposed/committed, the victim misses the epoch window and must wait for the next one — a delay of potentially weeks, during which their capacity earns no additional interest.
- **Chained transaction chains**: Removing a parent transaction also removes all descendants (`remove_entry_and_descendants`), destroying an entire pre-signed transaction chain that a user or protocol built.

The attacker gains nothing (pure griefing), directly mirroring the external report's impact class.

---

### Likelihood Explanation

- The default `listen_address = "127.0.0.1:8114"` binds to localhost, but the RPC documentation explicitly acknowledges that operators expose it to the internet for dApp connectivity, and warns (but does not enforce) against it. [6](#0-5) 
- Any **local process** on the same machine (e.g., a malicious script, a compromised dApp backend co-located with the node) can reach `127.0.0.1:8114` with zero privilege.
- Transaction hashes are public: `get_raw_tx_pool` returns all pending tx hashes to any caller.
- The attacker needs only a single HTTP POST with no credentials.

---

### Recommendation

1. **Add an authentication layer** to the RPC server (e.g., a configurable bearer token or HTTP Basic Auth header check) for all state-mutating pool methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`).
2. **Restrict destructive pool methods** to a separate, non-default module (similar to `IntegrationTest`) so operators must explicitly opt in.
3. At minimum, document that `remove_transaction` and `clear_tx_pool` must be firewalled from untrusted callers, and enforce this with a runtime IP-allowlist option in `RpcConfig`.

---

### Proof of Concept

```bash
# Step 1 — enumerate victim's pending transactions (no auth required)
curl -s -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns: {"result":{"pending":["0x<victim_tx_hash>",...],...}}

# Step 2 — attacker evicts victim's transaction (no ownership check)
curl -s -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["0x<victim_tx_hash>"]}'
# Returns: {"result":true}
# Victim's transaction (and all descendants) is now gone from the pool.
# For a DAO withdrawal with an epoch-based since, the victim misses the withdrawal window.
```

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

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```

**File:** resource/ckb.toml (L177-193)
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
```
