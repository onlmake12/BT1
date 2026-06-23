### Title
Permissionless `remove_transaction` RPC Allows Any Caller to Evict Another User's Pending Transaction from the Pool — (`rpc/src/module/pool.rs`)

### Summary
The `remove_transaction` RPC method in the `Pool` module accepts any `tx_hash` and immediately evicts that transaction plus all its descendants from the tx-pool. There is no ownership check, no signature requirement, and no verification that the caller is the original submitter of the transaction. Any unprivileged RPC caller can silently remove another user's pending or proposed transaction, forcing a resubmission and enabling targeted griefing or front-running of time-sensitive operations.

### Finding Description

The `Pool` RPC module is enabled by default in production CKB nodes. The `remove_transaction` method is declared at: [1](#0-0) 

Its implementation contains no caller identity check whatsoever: [2](#0-1) 

The call chain is:

1. `PoolRpcImpl::remove_transaction` calls `tx_pool_controller.remove_local_tx(tx_hash)`
2. `TxPoolController::remove_local_tx` sends a `RemoveLocalTx` message to the service actor: [3](#0-2) 

3. The service actor calls `process.remove_tx`, which removes the entry from the verify queue, orphan pool, and main pool — including all descendants via `remove_entry_and_descendants`: [4](#0-3) [5](#0-4) 

At no point is the caller's identity compared against the transaction's inputs, lock scripts, or any submitter record. The `Message` enum confirms `RemoveLocalTx` carries only the hash, with no principal field: [6](#0-5) 

The `Pool` module is listed among the default-enabled modules in the production config: [7](#0-6) 

The RPC documentation itself acknowledges that operators do expose the port beyond localhost and warns against it, but provides no enforcement: [8](#0-7) 

### Impact Explanation

An attacker with access to the RPC port (a local process, a co-tenant on the same machine, or any remote caller on a node that has exposed the RPC) can:

1. **Grief a victim's pending transaction**: observe the victim's `tx_hash` via `get_raw_tx_pool`, then call `remove_transaction(victim_tx_hash)`. The victim's transaction and all its descendants are silently dropped. The victim must resubmit, paying fees again and losing their position in the queue.
2. **Front-run time-sensitive transactions**: if a victim's transaction has a time-lock (`since` field) that is about to become valid, an attacker can evict it from the pool at the last moment, preventing timely confirmation.
3. **Cascade eviction**: because `remove_entry_and_descendants` is called, a single call can evict an entire chain of dependent transactions, amplifying the damage beyond the single targeted tx.

The impact is analogous to the original M-03 finding: no funds are directly stolen, but a victim's pending state is unilaterally altered by an unprivileged third party, causing denial of timely confirmation and potential economic harm (re-submission fees, missed time windows).

### Likelihood Explanation

- The `Pool` module is **enabled by default** in production.
- The RPC is bound to `127.0.0.1` by default, but the documentation explicitly acknowledges that operators expose it to broader networks, and the attacker profile "RPC caller" is explicitly in scope.
- The attack requires only knowledge of the victim's `tx_hash`, which is publicly observable via `get_raw_tx_pool` on the same node or via P2P relay.
- No cryptographic material, no privileged key, and no special role is required.

### Recommendation

Add an ownership check before allowing removal. Since CKB transactions are identified by their inputs (which are signed by the owner's lock script), the node should either:

1. Restrict `remove_transaction` to transactions that were submitted via `submit_local_tx` from the same session/connection (tracking submitter identity per tx), or
2. Remove `remove_transaction` from the default-enabled `Pool` module and move it to a privileged/admin-only module (e.g., `Debug` or `IntegrationTest`), or
3. Require the caller to provide a valid witness/signature over the `tx_hash` matching one of the transaction's input lock scripts before the eviction is performed.

### Proof of Concept

```
# Step 1: Victim submits a transaction
curl -X POST http://victim-node:8114 -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<victim_tx>],"id":1}'
# Returns: {"result": "0xVICTIM_TX_HASH"}

# Step 2: Attacker observes the pool (publicly readable)
curl -X POST http://victim-node:8114 -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":2}'
# Returns pool contents including 0xVICTIM_TX_HASH

# Step 3: Attacker evicts the victim's transaction with no credentials
curl -X POST http://victim-node:8114 -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xVICTIM_TX_HASH"],"id":3}'
# Returns: {"result": true}
# Victim's transaction and all descendants are now gone from the pool.
```

The victim's transaction is silently dropped. All descendant transactions chained on it are also removed via `remove_entry_and_descendants`. The victim must resubmit, losing queue priority and paying fees again.

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

**File:** tx-pool/src/service.rs (L120-120)
```rust
    RemoveLocalTx(Request<Byte32, bool>),
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

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** rpc/README.md (L4-5)
```markdown

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.
```
