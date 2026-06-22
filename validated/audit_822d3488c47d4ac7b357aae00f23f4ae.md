### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any Pending Transaction from the Pool - (File: rpc/src/module/pool.rs)

### Summary

The `remove_transaction` RPC method in the `Pool` module is publicly accessible to any RPC caller with no ownership or authorization check. Any caller who can reach the JSON-RPC endpoint can remove any pending transaction — including those submitted by other users — along with all its descendants. The `Pool` module is enabled by default in the production configuration, making this reachable without any privileged role.

### Finding Description

The `remove_transaction` method is declared as a public RPC endpoint in the `PoolRpc` trait and implemented in `PoolRpcImpl`:

```rust
// rpc/src/module/pool.rs:254-255
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

The implementation unconditionally forwards the call to the tx-pool service:

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

The service-level handler removes the transaction and all its descendants from the verify queue, orphan pool, and main pool with no caller identity check:

```rust
// tx-pool/src/process.rs:440-455
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

The `Pool` module is included in the default production module list:

```toml
# resource/ckb.toml:190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

There is no check that the caller submitted the transaction, owns any cell referenced by it, or holds any administrative credential. The function was intended to allow a node operator to manage their own mempool, but it is exposed as a general RPC method callable by any client that can reach the endpoint.

This is structurally identical to the `withdrawToken()` bug: a function meant to be called only by an authorized internal path (`withdraw()` / the node operator) is instead exposed as a public entry point with no authorization guard, allowing any caller to trigger the privileged state change.

### Impact Explanation

Any caller with access to the RPC endpoint can:

1. Enumerate all pending transactions via `get_raw_tx_pool`.
2. Call `remove_transaction` with any `tx_hash` to silently evict that transaction and all its descendants from the pool.
3. Repeat indefinitely to prevent a targeted transaction from ever being confirmed.

The cascading removal (`remove_entry_and_descendants`) amplifies the impact: removing a single high-value parent transaction also removes every child transaction that depends on it. This enables targeted transaction censorship against any user whose transactions pass through a node whose RPC is reachable by the attacker.

### Likelihood Explanation

The `Pool` module is enabled by default. While the RPC binds to `127.0.0.1:8114` by default, many operators expose it to broader networks for dApp integration, wallet connectivity, or monitoring. Any process or user that can reach the endpoint — including co-located services, shared hosting environments, or operators who have opened the port — can exploit this without any credential. The attack requires only knowledge of a target transaction hash, which is observable from the public mempool via `get_raw_tx_pool` on the same node.

### Recommendation

Add an authorization layer to `remove_transaction`. The simplest mitigation matching the spirit of the referenced fix is to move `remove_transaction` out of the default `Pool` module and into the `IntegrationTest` module (which already requires Dummy PoW and is not enabled in production), or introduce a separate privileged admin module that is disabled by default. Alternatively, restrict `remove_transaction` so it can only be called when the RPC is configured in an explicitly privileged mode, and document that enabling it grants callers the ability to evict any transaction regardless of origin.

### Proof of Concept

1. Start a CKB node with the default configuration (Pool module enabled).
2. User A submits a transaction via `send_transaction` → tx_hash `0xAAA...`.
3. Attacker (any RPC caller) calls `get_raw_tx_pool` to observe `0xAAA...` in the pending set.
4. Attacker calls:
   ```json
   {"id":1,"jsonrpc":"2.0","method":"remove_transaction","params":["0xAAA..."]}
   ```
5. Response: `{"result": true}`.
6. User A's transaction and all its descendants are silently removed from the pool. User A must resubmit, and the attacker can repeat step 4 indefinitely, achieving permanent transaction censorship with no privileged access. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```
