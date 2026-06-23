### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any User's Pending Transaction from the Pool — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in the Pool module accepts any `tx_hash` and removes the matching transaction (plus all its descendants) from the local tx pool with no ownership check, no authentication, and no authorization. Any process that can reach the RPC port — which is the Pool module enabled by default — can silently evict another user's pending transaction, including time-sensitive ones, causing missed confirmation windows or cascading removal of dependent transaction chains.

---

### Finding Description

`remove_transaction` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl`:

```rust
// rpc/src/module/pool.rs:662-669
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

There is no check that the caller submitted the transaction, no session token, no signature, and no identity verification of any kind. The call flows directly to `TxPoolController::remove_local_tx` → `TxService::remove_tx`:

```rust
// tx-pool/src/process.rs:440-456
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

`remove_tx` in turn calls `pool_map.remove_entry_and_descendants`, which cascades the removal to all child transactions:

```rust
// tx-pool/src/pool.rs:358-361
pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
    let entries = self.pool_map.remove_entry_and_descendants(id);
    !entries.is_empty()
}
```

The Pool module is enabled by default in the production config:

```toml
# resource/ckb.toml:190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

The RPC service builder (`rpc/src/service_builder.rs`) applies only coarse module-level enable/disable logic — there is no per-method access control, no caller identity, and no ownership enforcement anywhere in the call chain.

The same pattern applies to `clear_tx_pool` (line 684) and `clear_tx_verify_queue` (line 694), which wipe the entire pool or verify queue respectively, with identical absence of access control.

---

### Impact Explanation

An attacker with access to the RPC port can:

1. **Evict any specific pending transaction** by hash — including transactions submitted by other users or applications sharing the same node.
2. **Cascade-remove entire transaction chains** — removing a parent transaction also removes all descendants via `remove_entry_and_descendants`.
3. **Defeat time-sensitive transactions** — CKB supports `since`-field time locks; a transaction that must be confirmed within a specific epoch or timestamp window can be repeatedly evicted before it confirms, causing the window to expire.
4. **Wipe the entire mempool** via `clear_tx_pool` — all pending transactions from all users are discarded simultaneously.

The analog to the ERC20 `burn()` issue is exact: just as anyone could call `burn(account, amount)` on tokens locked in a contract (causing new tokens to be minted to the contract address, not the user), here anyone can call `remove_transaction(tx_hash)` on a transaction submitted by another user, causing that transaction to be silently dropped from the pool without the submitter's knowledge or consent.

---

### Likelihood Explanation

- The RPC binds to `127.0.0.1:8114` by default, but any co-located process — a wallet, a DApp backend, a compromised npm/cargo dependency, or another user on a shared VPS — can reach it without any credential.
- The Pool module is enabled by default; no operator action is required to expose `remove_transaction`.
- The attacker needs only the transaction hash, which is public (returned by `send_transaction` and visible in `get_raw_tx_pool`).
- The prompt explicitly lists "RPC caller" and "supported local CLI/RPC user" as valid unprivileged attacker profiles.
- No privileged key, no leaked credential, and no majority hashpower is required.

---

### Recommendation

1. **Add caller-identity enforcement**: Require the caller to prove ownership of at least one input cell in the transaction (e.g., by providing a signature over the tx hash with the lock key of an input), analogous to the ERC20 fix of requiring `msg.sender == account`.
2. **Restrict `remove_transaction` to the submitting session**: Track which RPC session (connection) submitted a transaction via `send_transaction` and only allow removal from the same session.
3. **Move destructive pool operations to a separate privileged module**: `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` should be gated behind a separate module (e.g., `Admin`) that is disabled by default and requires explicit operator opt-in, separate from the general-purpose `Pool` module.

---

### Proof of Concept

```bash
# Step 1: User A submits a valid transaction via send_transaction
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<tx_json>,"passthrough"],"id":1}'
# Response: {"result":"0xTX_HASH..."}

# Step 2: Attacker (any co-located process, no credentials needed) removes it
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xTX_HASH..."],"id":2}'
# Response: {"result":true}
# User A's transaction is now gone from the pool, along with all its descendants.

# Step 3 (escalation): Attacker wipes the entire pool
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":3}'
# Response: {"result":null}
# All pending transactions from all users are discarded.
```

**Root cause files:**
- [1](#0-0) 
- [2](#0-1) 
- [3](#0-2) 
- [4](#0-3) 
- [5](#0-4) 
- [6](#0-5)

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/src/service_builder.rs (L36-46)
```rust
macro_rules! set_rpc_module_methods {
    ($self:ident, $name:expr, $check:ident, $add_methods:ident, $methods:expr) => {{
        let mut meta_io = MetaIoHandler::default();
        $add_methods(&mut meta_io, $methods);
        if $self.config.$check() {
            $self.add_methods(meta_io);
        } else {
            $self.update_disabled_methods($name, meta_io);
        }
        $self
    }};
```
