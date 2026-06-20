### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any Pending Transaction from the Pool - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` JSON-RPC method in the `Pool` module accepts a caller-supplied `tx_hash` and immediately evicts the matching transaction (and all its descendants) from the tx pool. There is no authentication, no caller-identity check, and no validation that the caller is the original submitter of the transaction. Any process with access to the RPC endpoint can silently remove any other user's pending transaction.

---

### Finding Description

`remove_transaction` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl`:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The implementation delegates directly to `TxPoolController::remove_local_tx`, which calls `process.rs`'s `remove_tx`:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    // searches verify_queue, orphan pool, then main pool — no auth check anywhere
    ...
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.remove_tx(&id)
}
``` [2](#0-1) 

There is no authentication layer anywhere in the RPC stack. A grep for `authentication`, `auth_token`, `api_key`, `bearer`, or `Authorization` in `rpc/src/` returns zero matches. The `Pool` module is enabled by default in the standard node configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [3](#0-2) 

The RPC config struct has no authentication field at all: [4](#0-3) 

---

### Impact Explanation

Any caller with access to the RPC port can:

1. Enumerate pending transactions via `get_raw_tx_pool`.
2. Call `remove_transaction(tx_hash)` for any target transaction.
3. The target transaction **and all its descendants** are immediately evicted from the verify queue, orphan pool, and main pool.

The victim must resubmit, but the attacker can loop and re-evict. For time-sensitive transactions (those using `since`-locked inputs or time-bound DeFi operations on CKB), repeated eviction can cause the transaction to miss its valid window, resulting in direct financial loss. The `remove_entry_and_descendants` call means a single RPC call can cascade-remove an entire dependency chain. [5](#0-4) 

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, which limits remote exploitation. However:

- Any local process on the node machine (a co-hosted web app, a compromised dependency, a malicious script) can call the endpoint without any credential.
- The bounty scope explicitly includes "supported local CLI/RPC user" as a valid attacker.
- Node operators who expose the RPC to a LAN or the internet (the documentation warns against this but does not prevent it) face remote exploitation with no authentication barrier.
- The `Pool` module — which contains `remove_transaction` — is **on by default**; no special configuration is required to expose the attack surface. [6](#0-5) 

---

### Recommendation

**Short term:** Add an optional API-token or IP-allowlist check to destructive pool management methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`). At minimum, document that these methods carry no caller authentication and should be firewalled from untrusted callers.

**Long term:** Introduce a per-method privilege tier in `RpcConfig` (e.g., `read_only_methods` vs `admin_methods`) so operators can expose read RPCs publicly while restricting mutating pool operations to localhost or authenticated callers only.

---

### Proof of Concept

```bash
# Step 1: Alice submits a time-sensitive transaction
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<alice_tx>, "passthrough"]}'
# Returns: {"result": "0xALICE_TX_HASH..."}

# Step 2: Attacker (any local process) removes Alice's transaction — no credential needed
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["0xALICE_TX_HASH..."]}'
# Returns: {"result": true}

# Step 3: Alice's transaction and all descendants are gone from the pool.
# If the transaction had a since-lock window that has now expired, it cannot be resubmitted.
```

The `remove_transaction` RPC method signature and its unauthenticated nature are confirmed at: [7](#0-6) [1](#0-0)

### Citations

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
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

**File:** tx-pool/src/process.rs (L440-456)
```rust
    pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
        let id = ProposalShortId::from_tx_hash(&tx_hash);
        {
            let mut queue = self.verify_queue.write().await;
            if queue.remove_tx(&id).is_some() {
                return true;
            }
        }
        {
            let mut orphan = self.orphan.write().await;
            if orphan.remove_orphan_tx(&id).is_some() {
                return true;
            }
        }
        let mut tx_pool = self.tx_pool.write().await;
        tx_pool.remove_tx(&id)
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
