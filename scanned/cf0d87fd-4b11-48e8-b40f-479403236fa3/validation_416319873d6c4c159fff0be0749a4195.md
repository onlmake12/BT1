### Title
Unauthenticated RPC Caller Can Remove Any User's Pending Transaction from the Pool — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` and `clear_tx_pool` RPC methods in `rpc/src/module/pool.rs` perform no ownership or authorization check before deleting transactions from the tx pool. Any RPC caller — explicitly an in-scope attacker profile — can remove any other user's pending or proposed transaction, or wipe the entire pool, without any credential or proof of submission ownership. This is a direct structural analog to the reported Solidity pattern where an admin can delete allocations for any account: here, any caller can delete any user's pool state.

---

### Finding Description

**Root cause — `remove_transaction`:**

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

The implementation accepts an arbitrary `tx_hash` from the caller and immediately forwards it to `remove_local_tx`. There is no check that the caller submitted the transaction, owns any input cell in it, or holds any credential. The underlying pool operation `remove_tx` in `tx-pool/src/pool.rs` removes the target entry **and all descendant transactions** via `remove_entry_and_descendants`.

**Root cause — `clear_tx_pool`:**

```rust
// rpc/src/module/pool.rs  line 684-692
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
```

This wipes every pending, proposed, and gap transaction from every user simultaneously. No authorization gate exists.

**Code path:**

1. Attacker sends a JSON-RPC request: `{"method":"remove_transaction","params":["<victim_tx_hash>"]}`
2. `PoolRpcImpl::remove_transaction` is called with no auth check.
3. `TxPoolController::remove_local_tx` dispatches `Message::RemoveLocalTx`.
4. `TxPool::remove_tx` calls `pool_map.remove_entry_and_descendants`, which removes the target and all child transactions.
5. The victim's transaction (and any dependent chain) is silently evicted.

The same path applies to `clear_tx_pool` → `TxPool::clear` → `pool_map.clear()`.

---

### Impact Explanation

- **Targeted griefing**: An attacker can remove a specific user's pending or proposed transaction by hash. If the transaction is in the `Proposed` state (already included in a proposal), eviction during the proposal window causes it to miss its commit opportunity, forcing the user to resubmit and wait for a new proposal cycle.
- **Cascade deletion**: `remove_entry_and_descendants` removes the target and all transactions that depend on it, so a single call can evict an entire chain of dependent transactions belonging to multiple users.
- **Full pool wipe**: `clear_tx_pool` removes all transactions from all users in one call, causing a complete mempool reset with no on-chain record.
- **Repeated denial**: Because there is no rate limit or ownership gate, an attacker can loop the call to continuously evict a victim's resubmitted transactions, effectively blocking them from getting confirmed.

Impact: **3/5** — mempool-level state loss; transactions can be resubmitted but the attack is repeatable and can block time-sensitive operations (e.g., proposed transactions near their commit deadline).

---

### Likelihood Explanation

- The RPC server is in scope as an attacker entry point: the prompt explicitly lists "RPC caller" and "supported local CLI/RPC user" as valid profiles.
- No credential, key, or privileged access is required — only the ability to send a JSON-RPC request.
- The RPC binds to localhost by default, but many node operators expose it to LAN or public interfaces for dApp integration; in those deployments the attack is remotely reachable.
- The victim's `tx_hash` is publicly observable via `get_raw_tx_pool` or network relay, so the attacker does not need any secret information.

Likelihood: **3/5**

---

### Recommendation

1. **Restrict destructive pool RPCs** (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`) to a separate, authenticated RPC module or behind an operator-only token/IP allowlist.
2. **Add ownership verification** to `remove_transaction`: require the caller to provide a valid signature over the `tx_hash` using a key that controls at least one input cell of the transaction, or restrict the method to the node operator only.
3. At minimum, document that these methods must not be exposed on public-facing RPC interfaces and enforce this with a configuration guard.

---

### Proof of Concept

**Preconditions**: Victim submits transaction `T` (hash `0xABCD…`) to the pool via `send_transaction`. Attacker has RPC access (local or remote).

**Step 1 — Discover victim tx hash** (publicly available):
```json
{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}
```

**Step 2 — Remove victim's transaction with no credential**:
```json
{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["0xABCD..."]}
```

**Result**: `{"result": true}` — victim's transaction and all descendants are silently evicted from the pool. The victim must resubmit and wait for a new proposal cycle. The attacker can repeat this indefinitely.

**Step 3 — Full pool wipe (all users)**:
```json
{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}
```

**Result**: `{"result": null}` — every pending and proposed transaction from every user is deleted simultaneously. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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

**File:** rpc/src/module/pool.rs (L694-701)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }
```

**File:** tx-pool/src/pool.rs (L358-361)
```rust
    pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
        let entries = self.pool_map.remove_entry_and_descendants(id);
        !entries.is_empty()
    }
```

**File:** tx-pool/src/pool.rs (L516-522)
```rust
    pub(crate) fn clear(&mut self, snapshot: Arc<Snapshot>) {
        self.pool_map.clear();
        self.snapshot = snapshot;
        self.committed_txs_hash_cache = LruCache::new(COMMITTED_HASH_CACHE_SIZE);
        self.conflicts_cache = LruCache::new(CONFLICTES_CACHE_SIZE);
        self.conflicts_outputs_cache = lru::LruCache::new(CONFLICTES_INPUTS_CACHE_SIZE);
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
