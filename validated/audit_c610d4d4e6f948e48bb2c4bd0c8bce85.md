### Title
Missing Authorization Check on Privileged State-Mutating RPC Methods Allows Any Caller to Disable Networking, Wipe Mempool, or Manipulate Ban List - (File: rpc/src/module/net.rs, rpc/src/module/pool.rs)

---

### Summary
The CKB JSON-RPC server exposes several privileged, destructive state-changing methods — `set_network_active`, `clear_tx_pool`, `clear_tx_verify_queue`, `clear_banned_addresses`, `set_ban`, `add_node`, and `remove_node` — with zero caller authentication or authorization check. Any process that can reach the RPC port (a supported local CLI/RPC user, or any process on the same host) can invoke these methods unconditionally. This is the direct analog of the reported Solidity pattern: privileged operator functions (`addOperator`/`removeOperator`) that are missing the `onlyOwner` guard.

---

### Finding Description

The CKB RPC system has no built-in authentication or role-based access control. All callers — whether the legitimate node operator or any other local process — are treated identically. The following privileged methods perform irreversible or high-impact state mutations with no authorization gate:

**`set_network_active`** (`rpc/src/module/net.rs`, lines 772–774):
```rust
fn set_network_active(&self, state: bool) -> Result<()> {
    self.network_controller.set_active(state);
    Ok(())
}
```
This directly sets the `active: AtomicBool` flag in `NetworkState` (`network/src/network.rs`, line 87). Setting it to `false` disables all P2P message processing, completely isolating the node from the network. No check of any kind precedes this call.

**`clear_tx_pool`** (`rpc/src/module/pool.rs`, lines 684–692):
```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot)...
    Ok(())
}
```
Wipes the entire mempool unconditionally.

**`clear_banned_addresses`** (`rpc/src/module/net.rs`, lines 686–689):
```rust
fn clear_banned_addresses(&self) -> Result<()> {
    self.network_controller.clear_banned_addrs();
    Ok(())
}
```
Removes all IP bans, allowing previously-banned malicious peers to reconnect immediately.

**`set_ban`**, **`add_node`**, **`remove_node`**, **`clear_tx_verify_queue`** follow the same pattern — no authorization check before executing privileged network or pool state mutations.

By contrast, `send_alert` correctly enforces a 2-of-4 multisig check via `AlertVerifier::verify_signatures` (`util/network-alert/src/verifier.rs`, lines 33–64) before any state change. The privileged Net and Pool methods have no equivalent guard.

The RPC trait definitions confirm no middleware or per-method auth wrapper exists:
- `set_network_active` declared at `rpc/src/module/net.rs`, line 420
- `clear_tx_pool` declared at `rpc/src/module/pool.rs`, line 322
- `clear_banned_addresses` declared at `rpc/src/module/net.rs`, line 287

The `ServiceBuilder` mounts all methods from a module uniformly with no per-method privilege tier (`rpc/src/service_builder.rs`, lines 36–46).

---

### Impact Explanation

| Method | Impact |
|---|---|
| `set_network_active(false)` | Complete P2P isolation of the node; stops sync, relay, and all peer communication |
| `clear_tx_pool()` | Wipes entire mempool; all pending user transactions are silently dropped |
| `clear_banned_addresses()` | Removes all IP bans; previously-banned malicious/eclipse peers can immediately reconnect |
| `set_ban(ip, "insert", ...)` | Bans arbitrary IPs including legitimate peers, disrupting connectivity |
| `remove_node(peer_id)` | Disconnects any specific peer |
| `add_node(peer_id, addr)` | Forces connection to an attacker-controlled node |
| `clear_tx_verify_queue()` | Drops all transactions pending verification |

The most severe is `set_network_active(false)`: a single unauthenticated RPC call permanently halts all P2P activity until the operator manually re-enables it or restarts the node, causing complete network isolation and sync stall.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default. The attacker surface is any process running on the same host as the CKB node: a compromised dependency, a malicious script, a co-located application, or a local CLI user. This is explicitly listed as a supported attacker profile ("supported local CLI/RPC user"). No credentials, keys, or elevated OS privileges are required — only TCP reachability to the RPC port. The attack is a single HTTP POST with no preconditions.

---

### Recommendation

Implement a tiered authorization model for the RPC:

1. **Token-based authentication**: Require a secret bearer token (configured in `ckb.toml`) for all state-mutating methods. The token is checked in a middleware layer before dispatch.
2. **Method-level privilege tiers**: Classify methods as `read-only` (no auth required) vs. `privileged` (token required). At minimum, `set_network_active`, `clear_tx_pool`, `clear_tx_verify_queue`, `clear_banned_addresses`, `set_ban`, `add_node`, `remove_node` must be in the privileged tier.
3. **Analogy to `send_alert`**: The alert system already demonstrates the correct pattern — verify authorization before executing. Apply the same principle to Net and Pool mutation methods.

---

### Proof of Concept

No credentials required. From any process on the same host:

```bash
# Disable all P2P network activity — node is immediately isolated
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"set_network_active","params":[false],"id":1}'
# Returns: {"jsonrpc":"2.0","result":null,"id":1}

# Wipe the entire mempool
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
# Returns: {"jsonrpc":"2.0","result":null,"id":2}

# Remove all IP bans
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"clear_banned_addresses","params":[],"id":3}'
# Returns: {"jsonrpc":"2.0","result":null,"id":3}
```

Each call succeeds immediately with no authentication, no token, and no error. The node's P2P layer is disabled, its mempool is empty, and its ban list is cleared — all from a single unprivileged local process.