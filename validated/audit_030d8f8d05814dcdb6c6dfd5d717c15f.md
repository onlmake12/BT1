### Title
Permissionless Transaction Removal via `remove_transaction` RPC — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in `rpc/src/module/pool.rs` accepts only a transaction hash and removes the matching entry (plus all descendants) from the tx-pool without verifying that the caller is the original submitter of that transaction. Any RPC-reachable caller can silently evict any other user's pending transaction from the pool, directly analogous to the permissionless `carryVoteForward` pattern where a public function acts on another user's asset without an ownership check.

---

### Finding Description

The implementation of `remove_transaction` is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The function accepts a bare `tx_hash: H256` and immediately forwards it to `TxPoolController::remove_local_tx`, which calls down through `TxPoolService::remove_tx` → `TxPool::remove_tx` → `PoolMap::remove_entry_and_descendants`, unconditionally removing the transaction and every descendant that depends on it. [2](#0-1) [3](#0-2) 

There is no check that:
- `msg.sender` (the RPC caller) is the address that originally submitted the transaction, or
- the caller holds any lock-script key that authorizes spending the consumed cells, or
- any other ownership/authorization predicate.

The internal name `remove_local_tx` implies the function was designed for trusted local use, but the RPC layer imposes no such restriction. The Pool module is enabled by default in `ckb.toml` and the RPC server has no built-in authentication layer. [4](#0-3) [5](#0-4) 

---

### Impact Explanation

**Impact: Medium**

An unprivileged RPC caller can:

1. **Grief any pending transaction** — remove a victim's transaction (and all its children) from the pool, forcing the victim to resubmit and pay fees again.
2. **Block time-sensitive transactions** — repeatedly evict a transaction that must confirm within a deadline (e.g., a HTLC unlock, a DAO withdrawal, or a time-locked cell spend), causing it to miss its window.
3. **Manipulate mempool composition** — remove competing transactions to improve their own transaction's priority or fee-rate ranking without paying higher fees.
4. **Cascade removal** — because `remove_entry_and_descendants` is called, a single call targeting a parent transaction removes all child transactions that depend on it, amplifying the damage. [6](#0-5) 

---

### Likelihood Explanation

**Likelihood: High**

- The Pool RPC module is **enabled by default** in the shipped `ckb.toml`.
- The RPC server has **no authentication mechanism**; access control is entirely delegated to network-level configuration.
- Production deployments (exchanges, dApp backends, public RPC nodes) routinely expose the RPC port to the internet or to a shared internal network, as documented warnings in the README acknowledge this is a common risk.
- The attack requires only knowledge of a target transaction hash, which is publicly observable on the P2P network the moment the transaction is relayed.
- No special keys, privileged roles, or on-chain assets are required. [7](#0-6) 

---

### Recommendation

Add an ownership/authorization check before executing the removal. The most principled fix is to record the submitter identity at submission time and enforce it at removal time. A simpler short-term mitigation is to restrict `remove_transaction` to a separate, non-default RPC module (e.g., a new `LocalPool` module that is disabled unless explicitly enabled), or to gate it behind the same mechanism used for other sensitive operations.

Concretely, in `rpc/src/module/pool.rs`, the `remove_transaction` handler should verify that the caller's IP/token matches the submitter recorded when the transaction was accepted via `submit_local_tx`, or alternatively move the method out of the default-enabled `Pool` module into an opt-in administrative module. [8](#0-7) 

---

### Proof of Concept

**Preconditions:**
- A CKB node with the default `Pool` RPC module enabled.
- The RPC port is reachable by the attacker (either exposed publicly or attacker has local access).
- Victim has submitted a pending transaction `T` with hash `H`.

**Steps:**

1. Attacker observes transaction hash `H` from the P2P relay network (or from `get_raw_tx_pool`).
2. Attacker sends:
   ```json
   {
     "id": 1,
     "jsonrpc": "2.0",
     "method": "remove_transaction",
     "params": ["0x<H>"]
   }
   ```
3. Node responds `{"result": true}` — transaction `T` and all its descendants are removed from the pool with no authorization check.
4. Victim's transaction is gone; they must resubmit and pay fees again.
5. Attacker can repeat this indefinitely to permanently prevent the transaction from confirming. [1](#0-0) [2](#0-1)

### Citations

**File:** rpc/src/module/pool.rs (L220-260)
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

    /// Returns the transaction pool information.
    ///
    /// ## Examples
    ///
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

**File:** rpc/README.md (L4-6)
```markdown

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

```
