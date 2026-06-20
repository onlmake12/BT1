### Title
Missing Access Control on `remove_transaction` and `clear_tx_pool` RPC Methods Allows Unauthenticated Transaction Pool Manipulation — (`rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC `Pool` module exposes `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` as publicly callable methods with no authentication or authorization check. Any caller who can reach the RPC endpoint — including any process on the same host or any network client when the RPC is exposed — can silently remove arbitrary transactions from the pool or wipe it entirely. This is a direct analog to the Rubicon Market finding: privileged state-manipulation functions that should be restricted to authorized users are callable by anyone.

---

### Finding Description

The `Pool` RPC module is enabled by default in the standard node configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [1](#0-0) 

Within that module, three methods perform destructive, privileged mutations on the transaction pool:

**`remove_transaction`** — removes a transaction and all its descendants from the pool:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
``` [2](#0-1) 

Implementation delegates directly to `tx_pool.remove_local_tx(tx_hash.into())` with no caller identity check:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| { ... })
}
``` [3](#0-2) 

**`clear_tx_pool`** — wipes the entire mempool:

```rust
#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;
``` [4](#0-3) 

Implementation calls `tx_pool.clear_pool(snapshot)` unconditionally:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot).map_err(|err| ...)?;
    Ok(())
}
``` [5](#0-4) 

**`clear_tx_verify_queue`** — clears the pending verification queue:

```rust
#[rpc(name = "clear_tx_verify_queue")]
fn clear_tx_verify_queue(&self) -> Result<()>;
``` [6](#0-5) 

None of these three methods perform any authentication, IP allowlist check, token validation, or caller identity verification. The `ServiceBuilder` mounts them unconditionally whenever the `Pool` module is enabled:

```rust
set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
``` [7](#0-6) 

The `RpcConfig` struct has no authentication fields at all — the only access control is the binary choice of whether a module is enabled: [8](#0-7) 

---

### Impact Explanation

An attacker who can reach the RPC endpoint can:

1. **Targeted transaction censorship**: Call `remove_transaction(tx_hash)` to silently evict any specific pending or proposed transaction from the pool before it is included in a block. The transaction is gone from the local node's pool and will not be relayed further from that node. A user whose transaction is repeatedly removed will experience indefinite confirmation delay with no error feedback.

2. **Full mempool wipe**: Call `clear_tx_pool()` to atomically destroy all pending and proposed transactions. Every user who submitted a transaction to this node loses their pending state. Fees already paid in replaced-by-fee scenarios are wasted. Downstream services (wallets, dApps) that rely on the node's pool state are disrupted.

3. **Verification stall**: Call `clear_tx_verify_queue()` to drop all transactions currently awaiting script verification, silently discarding work already in progress.

These operations bypass the normal pool eviction logic (fee-rate-based eviction, RBF, ancestor-count limits) and allow an unprivileged caller to manipulate pool state in ways the protocol does not intend to expose publicly.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default: [9](#0-8) 

However:
- Any process running on the same host as the node (a co-located wallet, dApp backend, or malicious script) can call these methods with a plain HTTP POST — no credentials needed.
- Many node operators expose the RPC to a LAN or public network for wallet and dApp connectivity. The documentation warns against this but does not enforce it.
- The `Pool` module is in the default module list, so these methods are active on every standard node deployment.
- No exploit tooling is required: a single `curl` command suffices.

The attacker profile is "RPC caller" and "supported local CLI/RPC user," both explicitly in scope.

---

### Recommendation

1. **Introduce per-method or per-module authentication tokens** in `RpcConfig`. Methods that mutate pool state (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`) should require a bearer token or HTTP Basic Auth credential that the operator sets at startup.

2. **Separate destructive pool-management methods into a restricted sub-module** (e.g., `PoolAdmin`) that is disabled by default and requires explicit opt-in, analogous to how `IntegrationTest` is disabled by default.

3. **At minimum, document these methods as operator-only and add a runtime IP allowlist check** so that only requests from a configurable set of trusted addresses are accepted for these endpoints.

---

### Proof of Concept

Assuming the node is running with default config and the Pool module enabled:

```bash
# Remove a specific transaction from the pool (no credentials required)
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{
    "id": 1,
    "jsonrpc": "2.0",
    "method": "remove_transaction",
    "params": ["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"]
  }'
# Returns: {"id":1,"jsonrpc":"2.0","result":true}

# Wipe the entire mempool (no credentials required)
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"id":2,"jsonrpc":"2.0","result":null}
```

Any process on the same host, or any network client if the operator has exposed the RPC port, can execute these calls. The node provides no challenge, token, or signature requirement. The transaction is permanently removed from the local pool with no audit trail beyond an internal log line. [10](#0-9)

### Citations

**File:** resource/ckb.toml (L182-182)
```text
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
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

**File:** rpc/src/module/pool.rs (L662-701)
```rust
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
        let tx_pool = self.shared.tx_pool_controller();

        tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
            error!("Send remove_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })
    }

    fn tx_pool_info(&self) -> Result<TxPoolInfo> {
        let tx_pool = self.shared.tx_pool_controller();
        let get_tx_pool_info = tx_pool.get_tx_pool_info();
        if let Err(e) = get_tx_pool_info {
            error!("Send get_tx_pool_info request error {}", e);
            return Err(RPCError::ckb_internal_error(e));
        };

        let tx_pool_info = get_tx_pool_info.unwrap();

        Ok(tx_pool_info.into())
    }

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

**File:** rpc/src/service_builder.rs (L76-76)
```rust
        set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
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
