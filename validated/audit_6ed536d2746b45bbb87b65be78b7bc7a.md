### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Arbitrary Pending Transactions — (`File: rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in CKB's Pool module accepts only a transaction hash and performs no ownership or authorization check. Any caller with access to the RPC endpoint can silently evict any other user's pending transaction — and all its dependents — from the tx-pool. This is a direct structural analog to STK-2: just as the `stake` function allowed acting on behalf of any address, `remove_transaction` allows acting against any transaction without being its submitter.

---

### Finding Description

`remove_transaction` is declared in `rpc/src/module/pool.rs` and implemented as:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The only parameter is `tx_hash`. There is no check that the caller submitted the transaction, owns any of its inputs, or has any relationship to it whatsoever. The call flows directly to `TxPoolController::remove_local_tx`, which calls `TxPool::remove_tx` → `pool_map.remove_entry_and_descendants`, unconditionally removing the target transaction and every descendant.

The RPC server (`rpc/src/server.rs`) mounts the handler with `CorsLayer::permissive()` and no authentication middleware. The `ServiceBuilder` (`rpc/src/service_builder.rs`) enables the entire Pool module as a unit via `pool_enable()` — there is no per-method access control. The Pool module is included in the default `modules` list in `resource/ckb.toml` (`"Pool"` is listed), so `remove_transaction` is active in a standard node deployment.

---

### Impact Explanation

An attacker who can reach the RPC endpoint (any local process by default; any remote host if the operator exposes the port, which the documentation explicitly warns against but does not prevent) can:

1. **Censor specific transactions**: Enumerate the tx-pool via `get_raw_tx_pool`, identify a victim's transaction hash, and call `remove_transaction` to evict it and all its descendants.
2. **Cascade eviction**: Because `remove_entry_and_descendants` is called, a single call removes an entire dependency chain, amplifying the damage.
3. **Sustained griefing**: The victim must resubmit; the attacker can immediately call `remove_transaction` again. For time-sensitive operations (DAO withdrawal deadlines, RBF fee-bump races, time-locked scripts), repeated eviction can cause the victim to miss a critical window, resulting in financial loss.
4. **No trace of attacker identity**: The RPC carries no caller identity; the removal is indistinguishable from a legitimate node operator action.

---

### Likelihood Explanation

- The Pool RPC module is **enabled by default** in `resource/ckb.toml`.
- The default bind address is `127.0.0.1:8114`, so any process on the same host — including browser-based scripts via CORS (the server uses `CorsLayer::permissive()`), malicious co-located software, or a compromised dApp — can reach it without credentials.
- The CKB documentation explicitly warns that exposing the RPC to arbitrary machines is "dangerous and strongly discouraged," confirming that real deployments do expose it beyond localhost.
- The attacker needs only a valid tx hash, which is publicly observable from `get_raw_tx_pool` or the P2P relay network.
- No cryptographic material, privileged key, or special role is required.

---

### Recommendation

Add caller-identity verification before allowing removal. Concretely:

1. Require that `remove_transaction` is only callable by a caller who can prove ownership of at least one input cell in the transaction (e.g., by signing a challenge with the corresponding lock-script key), or restrict the method to a separately authenticated administrative endpoint.
2. At minimum, move `remove_transaction` behind a separate, opt-in RPC module (e.g., `Admin`) that operators must explicitly enable and protect with network-level access controls, rather than bundling it in the general-purpose `Pool` module.
3. Document clearly that `remove_transaction` has no ownership check and should never be exposed on a public or shared RPC endpoint.

---

### Proof of Concept

```
# Step 1: Observe victim's pending transaction
curl -s http://127.0.0.1:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'
# Returns list of tx hashes in the pool.

# Step 2: Remove victim's transaction (and all descendants) with no credentials
curl -s http://127.0.0.1:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction",
       "params":["<victim_tx_hash>"],"id":2}'
# Returns: {"result": true}
# Victim's transaction is now gone from the pool.
```

The attacker repeats Step 2 each time the victim resubmits, indefinitely, with no authentication required.

---

**Root cause references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
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

**File:** rpc/src/service_builder.rs (L64-77)
```rust
    /// Mounts methods from module Pool if it is enabled in the config.
    pub fn enable_pool(
        mut self,
        shared: Shared,
        extra_well_known_lock_scripts: Vec<Script>,
        extra_well_known_type_scripts: Vec<Script>,
    ) -> Self {
        let methods = PoolRpcImpl::new(
            shared,
            extra_well_known_lock_scripts,
            extra_well_known_type_scripts,
        );
        set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
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
