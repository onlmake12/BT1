### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any Pending Transaction from the Pool — (`rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` RPC method accepts a `tx_hash` and unconditionally removes the matching transaction (and all its descendants) from the tx pool. There is no caller-identity check, no ownership record, and no authentication layer on the RPC server. Any party that can reach the RPC endpoint — including any local process or any remote client when the operator has exposed the port — can silently evict a transaction submitted by a different user, without that user's knowledge or consent.

### Finding Description

**Root cause — `rpc/src/module/pool.rs`, `remove_transaction`:**

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The function takes only a `tx_hash`; it never records who submitted the transaction, never checks whether the caller is the original submitter, and never requires any credential. The underlying service call is equally unchecked:

```rust
pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
    send_message!(self, RemoveLocalTx, tx_hash)
}
``` [2](#0-1) 

The pool-level removal cascades to all descendants:

```rust
pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
    let mut removed_ids = vec![id.to_owned()];
    removed_ids.extend(self.calc_descendants(id));
    ...
}
``` [3](#0-2) 

**RPC has no authentication layer.** The `Config` struct for the RPC module contains no token, API key, or per-caller identity field: [4](#0-3) 

The only protection is the default bind address (`127.0.0.1:8114`), which is a deployment convention, not an access-control mechanism: [5](#0-4) 

The RPC documentation itself acknowledges the risk but provides no mitigation inside the code: [6](#0-5) 

**Exploit flow:**

1. User A submits a time-sensitive transaction (e.g., a DAO withdrawal, an RBF replacement, a DEX trade) via `send_transaction`. The tx hash is public the moment it is broadcast or visible in `get_raw_tx_pool`.
2. Attacker B — any process on the same host, or any remote client if the operator has bound the RPC to a non-loopback address — calls `remove_transaction` with that hash.
3. The transaction and all its descendants are silently removed from the pool. No notification is sent to User A.
4. User A's transaction never gets confirmed. If the operation was time-sensitive (e.g., a `since`-locked DAO withdrawal window, an RBF fee bump racing a deadline), the opportunity is permanently lost.

### Impact Explanation

- **Unauthorized state change:** Any RPC-reachable caller can remove any pending transaction without the submitter's consent.
- **Financial harm:** Time-sensitive transactions (DAO withdrawals, RBF replacements, arbitrage) can be permanently invalidated by a third party. The submitter loses the economic opportunity; in DAO withdrawal cases this can mean missing an unlock window entirely.
- **Cascading removal:** `remove_entry_and_descendants` removes the target transaction *and all dependent transactions*, amplifying the damage from a single call.
- **No audit trail:** The removal is silent; the original submitter has no way to distinguish a deliberate eviction from a normal pool expiry.

### Likelihood Explanation

- The `Pool` module is enabled by default in `ckb.toml` (`modules = ["Net", "Pool", "Miner", ...]`). [7](#0-6) 
- Many operators expose the RPC port to non-loopback addresses for wallets, dApps, and block explorers. The documentation warns against this but does not enforce it.
- The tx hash of any pending transaction is publicly observable via `get_raw_tx_pool`, so the attacker needs no privileged knowledge.
- The attack requires a single unauthenticated HTTP POST — no keys, no signatures, no special role.

### Recommendation

1. **Track submitter identity per transaction.** When a transaction is admitted via `submit_local_tx`, record the caller's identity (e.g., a session token or IP-based token). `remove_transaction` should verify the caller matches the recorded submitter before proceeding.
2. **Alternatively, gate `remove_transaction` behind a separate privileged RPC module** (e.g., a new `Admin` module disabled by default), so operators who expose the `Pool` module to external clients do not inadvertently expose removal capability.
3. **Minimum viable fix:** Add an authentication token to the RPC config (similar to Bitcoin Core's `rpcauth`) and require it for all state-mutating pool operations (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`).

### Proof of Concept

```
# Step 1: User A submits a transaction
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<tx_json>,"passthrough"],"id":1}'
# Returns: {"result": "0xTX_HASH", ...}

# Step 2: Attacker B (any process that can reach the RPC port) lists the pool
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":2}'
# Returns tx hashes of all pending transactions

# Step 3: Attacker B removes User A's transaction — no credential required
curl -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xTX_HASH"],"id":3}'
# Returns: {"result": true, ...}
# User A's transaction and all its descendants are now gone from the pool.
```

Expected outcome: `remove_transaction` returns `true`; the transaction is evicted; User A's operation fails silently with no recourse.

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```

**File:** tx-pool/src/component/pool_map.rs (L252-265)
```rust
    pub(crate) fn remove_entry_and_descendants(&mut self, id: &ProposalShortId) -> Vec<TxEntry> {
        let mut removed_ids = vec![id.to_owned()];
        removed_ids.extend(self.calc_descendants(id));

        // update links state for remove, so that we won't update_descendants_index_key in remove_entry
        for id in &removed_ids {
            self.remove_entry_links(id);
        }

        removed_ids
            .iter()
            .filter_map(|id| self.remove_entry(id))
            .collect()
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

**File:** rpc/README.md (L1-9)
```markdown
# CKB JSON-RPC Protocols

The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers.

See [list of projects](https://github.com/topics/ckb-rpc-proxy) to setup the proxy for the RPC server.
```
