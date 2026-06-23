### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Any Reachable Caller to Arbitrarily Purge the Transaction Pool — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` and `clear_tx_pool` RPC methods in `rpc/src/module/pool.rs` carry no authentication or authorization guard. Any caller who can reach the RPC endpoint — including an unprivileged external attacker if the operator has bound the RPC to a non-loopback address — can silently remove any specific transaction or wipe the entire mempool. This is a direct structural analog to H-10: a state-modifying function that should be operator-only is callable by anyone, with no check standing in the way.

---

### Finding Description

**Root cause — no access control on destructive pool operations.**

`remove_transaction` is defined in the `PoolRpc` trait and implemented without any caller-identity check:

```rust
// rpc/src/module/pool.rs  lines 662-669
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

`clear_tx_pool` is equally unguarded:

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

Neither function inspects who is calling it. The RPC server has no authentication middleware. The only barrier is network-level reachability.

**Reachability.** The default configuration binds the RPC to `127.0.0.1:8114`. However:

- The configuration file explicitly warns operators: *"Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged. Please strictly limit the access to only trusted machines."* — this warning exists precisely because operators routinely bind the RPC to `0.0.0.0` or a public interface.
- The code supports any bind address; there is no enforcement.
- No API-key, token, or IP-allowlist check exists anywhere in the RPC stack.

Once the RPC is reachable (a common real-world deployment scenario), the attacker needs only to know a transaction hash (observable from the P2P relay network) and issue a single JSON-RPC call.

**Exploit flow:**

1. Attacker observes a target transaction hash via the P2P relay protocol (transactions are broadcast publicly).
2. Attacker sends `{"method":"remove_transaction","params":["<tx_hash>"]}` to the exposed RPC port.
3. `remove_local_tx` is called on the tx-pool service, which removes the transaction **and all its descendants** from pending, proposed, and orphan pools.
4. The transaction is silently gone; the submitter receives no notification.
5. For maximum impact, attacker calls `clear_tx_pool` to wipe the entire mempool in one call.

The internal path through `process.rs` confirms the removal is unconditional:

```rust
// tx-pool/src/process.rs  lines 440-455
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    { let mut queue = self.verify_queue.write().await;
      if queue.remove_tx(&id).is_some() { return true; } }
    { let mut orphan = self.orphan.write().await;
      if orphan.remove_orphan_tx(&id).is_some() { return true; } }
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.remove_tx(&id)
}
```

No ownership check, no signature, no role gate — the call succeeds for any caller.

---

### Impact Explanation

- **Transaction censorship**: An attacker can target specific users' transactions (e.g., a high-value DeFi operation or a time-sensitive unlock) and repeatedly remove them from the pool before they are committed, indefinitely blocking confirmation.
- **Mining revenue disruption**: Clearing the mempool removes all pending fee-bearing transactions, forcing miners to mine empty or near-empty blocks and forfeiting fee income.
- **Descendant cascade**: `remove_entry_and_descendants` removes the target transaction *and every transaction that depends on it*, amplifying the damage from a single call.
- **No recovery signal**: The submitter's wallet sees the transaction as "pending" until it re-queries; there is no rejection notification, making the attack stealthy.

---

### Likelihood Explanation

- The RPC module is enabled by default with the `Pool` module active.
- Operators frequently expose the RPC to non-loopback addresses for remote miner connectivity (`get_block_template` / `submit_block` are in the same server).
- The attacker needs only network access to the RPC port and knowledge of a transaction hash (freely observable on the P2P network).
- No cryptographic material, privileged key, or insider access is required.
- The attack is repeatable and cheap (a single HTTP POST per removal).

---

### Recommendation

1. **Add an authentication layer to the RPC server** (e.g., Bearer token / API key checked in middleware before dispatching any method). All state-mutating methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`, `set_ban`, `clear_banned_addresses`) must require a valid credential.
2. **Separate read-only and write RPC endpoints** (different ports/paths), so operators can safely expose read methods without exposing destructive ones.
3. **Add an IP allowlist** for state-mutating RPC calls as a defense-in-depth measure.
4. **Document the risk prominently** in the default `ckb.toml` next to the `listen_address` field, not just in a README.

---

### Proof of Concept

**Precondition**: Node RPC is bound to `0.0.0.0:8114` (common for remote miner setups).

**Step 1** — Observe a pending transaction hash from the P2P relay network (any connected peer sees relayed transactions).

**Step 2** — Issue the removal call:

```bash
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "method": "remove_transaction",
    "params": ["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],
    "id": 1
  }'
```

**Expected result**: `{"result": true}` — the transaction and all its descendants are removed from the pool with no authentication required.

**Step 3** — For full pool wipe:

```bash
curl -s -X POST http://<node-ip>:8114/ \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
```

**Expected result**: `{"result": null}` — entire mempool cleared.

---

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** rpc/src/module/pool.rs (L220-255)
```rust
    /// Removes a transaction and all transactions which depends on it from tx pool if it exists.
    ///
    /// ## Params
    ///
    /// * `tx_hash` - Hash of a transaction.
    ///
    /// ## Returns
    ///
    /// If the transaction exists, return true; otherwise, return false.
    ///
    /// ## Examples
    ///
    /// Request
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "method": "remove_transaction",
    ///   "params": [
    ///     "0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"
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
    ///   "result": true
    /// }
    /// ```
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

**File:** tx-pool/src/process.rs (L440-455)
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
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```

**File:** resource/ckb.toml (L181-193)
```text
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
# }}

# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760

# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
