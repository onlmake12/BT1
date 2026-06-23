### Title
`remove_transaction` RPC Allows Any Caller to Remove Transactions Submitted by Other Users — (File: `rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` JSON-RPC method in the Pool module carries no caller authentication and no ownership check. Any RPC caller who can reach the endpoint can remove **any** transaction from the tx-pool — including transactions submitted by completely unrelated users — by supplying only the public transaction hash. This is a direct structural analog to the PermissionRegistry `setPermission()` bug: a privileged (or in CKB's case, entirely unprivileged) caller can mutate state that belongs to another party.

---

### Finding Description

`remove_transaction` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl`:

```rust
// rpc/src/module/pool.rs  line 254-255
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

The implementation unconditionally forwards the call to the tx-pool service:

```rust
// rpc/src/module/pool.rs  line 662-669
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

`remove_local_tx` dispatches to `TxPoolService::remove_tx`, which removes the entry from the verify queue, orphan pool, and main pool without any check on who originally submitted the transaction:

```rust
// tx-pool/src/process.rs  line 440-455
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

The RPC module system only enforces **module-level** enable/disable (whether the `Pool` module is listed in `rpc.modules`); there is no per-caller identity, no API key, and no check that the caller is the original submitter of the transaction:

```rust
// util/app-config/src/configs/rpc.rs  line 80-82
pub fn pool_enable(&self) -> bool {
    self.modules.contains(&Module::Pool)
}
```

The `Pool` module is **enabled by default** in the production `ckb.toml`:

```toml
# resource/ckb.toml  line 190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

Transaction hashes are public (visible via `get_raw_tx_pool`, network relay, block explorers). Any caller who can reach the RPC endpoint therefore has everything needed to evict any pending or proposed transaction submitted by any other user.

---

### Impact Explanation

An attacker who can reach the RPC endpoint can:

1. **Selectively censor transactions** — repeatedly call `remove_transaction` on a target user's tx hash immediately after it is resubmitted, preventing it from ever reaching the proposed or committed state.
2. **Disrupt time-sensitive operations** — DeFi settlements, DAO votes, or time-locked withdrawals that depend on a transaction being committed within a specific block window can be permanently blocked if the attacker removes the transaction faster than the victim can resubmit.
3. **Combine with `clear_tx_pool`** — the same module exposes `clear_tx_pool()` with identical absence of authentication, allowing bulk eviction of all pending transactions in a single call.

The same structural absence of ownership enforcement exists in `clear_tx_verify_queue`.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default. However:

- Many node operators expose the RPC publicly (the documentation explicitly warns about this, implying it is a common and supported deployment pattern).
- On a shared host (VPS, cloud instance with multiple tenants or processes), any local process can reach `127.0.0.1:8114` without any credentials.
- The `Pool` module is enabled by default; no operator action is required to expose `remove_transaction`.
- Transaction hashes are fully public, so the attacker needs no insider knowledge.

The attacker profile is explicitly in scope: **"RPC caller"** and **"supported local CLI/RPC user"** are both listed as valid attacker profiles in the project's own `RESEARCHER.md`.

---

### Recommendation

Add caller-identity enforcement to destructive Pool RPC methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`). Options include:

- Require a shared secret / bearer token in the HTTP header, validated in the RPC server middleware.
- Restrict these methods to a separate, non-default RPC module (e.g., `PoolAdmin`) that operators must explicitly enable and bind to a non-public address.
- At minimum, document clearly that enabling the `Pool` module on a publicly reachable address grants any caller the ability to evict any user's transactions.

---

### Proof of Concept

```bash
# Step 1: victim submits a transaction
curl -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<tx_data>,"passthrough"],"id":1}'
# Response: {"result":"0xabc...def"}   <-- tx_hash is now public

# Step 2: attacker (any RPC caller, no credentials) removes it
curl -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xabc...def"],"id":2}'
# Response: {"result":true}

# Step 3: victim's transaction is gone; if attacker loops, it never confirms
```

The call path is: HTTP request → `PoolRpcImpl::remove_transaction` (`rpc/src/module/pool.rs:662`) → `TxPoolController::remove_local_tx` (`tx-pool/src/service.rs:273`) → `TxPoolService::remove_tx` (`tx-pool/src/process.rs:440`) → `TxPool::remove_tx` (`tx-pool/src/pool.rs:358`). No authentication or ownership gate exists at any step. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```

**File:** util/app-config/src/configs/rpc.rs (L80-82)
```rust
    pub fn pool_enable(&self) -> bool {
        self.modules.contains(&Module::Pool)
    }
```

**File:** resource/ckb.toml (L189-191)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
```
