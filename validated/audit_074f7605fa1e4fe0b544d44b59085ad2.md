### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any User's Pending Transaction from the Pool — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method accepts a caller-supplied `tx_hash` and unconditionally removes the matching transaction (and all its descendants) from the tx-pool. There is no ownership check, no authentication, and no authorization gate. Any caller who can reach the RPC endpoint can silently evict any other user's pending transaction. The RPC server is configured with `CorsLayer::permissive()`, which widens the reachable attacker surface beyond the local machine.

---

### Finding Description

`remove_transaction` in `rpc/src/module/pool.rs` is declared as:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

Its implementation is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

There is no check that the caller owns the transaction, submitted it, or has any relationship to it. The `tx_hash` is entirely attacker-controlled. The call chain is:

`remove_transaction` → `TxPoolController::remove_local_tx` → `TxPoolService::remove_tx` → `TxPool::remove_tx` → `PoolMap::remove_entry_and_descendants` [2](#0-1) [3](#0-2) [4](#0-3) 

The RPC server is built with no authentication middleware and explicitly uses `CorsLayer::permissive()`:

```rust
let app = Router::new()
    ...
    .layer(CorsLayer::permissive())
``` [5](#0-4) 

This means any HTTP origin (including a malicious web page loaded in a browser on the same machine as a local node) can issue cross-origin POST requests to the RPC endpoint and call `remove_transaction` with an arbitrary hash.

The same pattern applies to `clear_tx_pool`, which removes every transaction in the pool with no access control:

```rust
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.clear_pool(snapshot)...
}
``` [6](#0-5) 

Both methods are part of the standard `Pool` RPC module, enabled by default when `pool_enable` is set in config: [7](#0-6) 

---

### Impact Explanation

An attacker who can reach the RPC endpoint (publicly exposed node, or via CORS from a malicious web page against a local node) can:

1. **Targeted eviction**: Call `remove_transaction(victim_tx_hash)` to silently drop any specific user's pending transaction and all its descendants from the pool. The victim must detect the drop and resubmit.
2. **Time-window exploitation**: For transactions that use `since`-based time locks (absolute block height or epoch), evicting the transaction at the right moment can cause the user to miss the valid inclusion window permanently, since the `since` constraint may no longer be satisfiable after the window closes.
3. **Descendant cascade**: Because `remove_entry_and_descendants` is called, a single call evicts an entire dependency chain, amplifying the disruption.
4. **Full pool wipe**: `clear_tx_pool` removes every pending and proposed transaction in one call, causing a complete mempool reset for all users of the node. [8](#0-7) 

---

### Likelihood Explanation

- **Publicly exposed RPC**: Many CKB node operators expose the RPC on `0.0.0.0:8114` to serve dApps or wallets. Any internet client can call `remove_transaction` with no credential.
- **CORS-based local attack**: Even for nodes bound to `127.0.0.1`, `CorsLayer::permissive()` allows a malicious web page to issue cross-origin fetch requests to `http://localhost:8114`. The attacker only needs to lure the node operator to visit a malicious URL.
- **Transaction hashes are public**: The victim's `tx_hash` is observable via `get_raw_tx_pool` or by watching the P2P relay network, so the attacker does not need any privileged information.

---

### Recommendation

1. **Add an authentication layer** to the RPC server (API token, IP allowlist, or mutual TLS) before any state-mutating methods are reachable.
2. **Restrict `remove_transaction` and `clear_tx_pool`** to a separate, non-public RPC port (analogous to how `process_block_without_verify` is gated to `integration_test_enable`).
3. **Replace `CorsLayer::permissive()`** with an explicit allowlist of trusted origins to eliminate the CORS-based local attack surface.
4. **Require ownership proof**: Before removing a transaction, verify that the caller can demonstrate knowledge of the transaction's witness (or restrict the call to the submitting peer identity).

---

### Proof of Concept

**Precondition**: Alice's CKB node has the Pool RPC module enabled and the RPC is reachable (publicly or via CORS from a browser).

1. Alice submits a transaction via `send_transaction`. Her `tx_hash` is returned and is also visible to anyone via `get_raw_tx_pool`.
2. Bob (attacker) sends:
   ```json
   {
     "id": 1,
     "jsonrpc": "2.0",
     "method": "remove_transaction",
     "params": ["<alice_tx_hash>"]
   }
   ```
   to Alice's RPC endpoint with no credentials.
3. The node calls `remove_local_tx(alice_tx_hash)` → `remove_tx` → `remove_entry_and_descendants`, evicting Alice's transaction and all its children from the pool.
4. Alice's transaction is silently dropped. If it had a `since`-based time constraint that expires before she resubmits, the transaction becomes permanently invalid.
5. Bob can repeat this in a loop, preventing Alice's transaction from ever being confirmed on this node. [9](#0-8) [3](#0-2)

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
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

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
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

**File:** rpc/src/service_builder.rs (L65-77)
```rust
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
