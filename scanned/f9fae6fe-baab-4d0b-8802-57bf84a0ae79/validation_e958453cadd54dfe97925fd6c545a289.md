### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Arbitrary Pending Transactions — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` JSON-RPC endpoint accepts an arbitrary `tx_hash` parameter and unconditionally removes the matching transaction and all its descendants from the transaction pool. There is no authorization check verifying the caller has any relationship to the transaction. Any process that can reach the RPC port — including any local process or any remote client if the RPC is network-exposed — can silently evict any pending transaction submitted by any other party.

---

### Finding Description

The implementation in `PoolRpcImpl::remove_transaction` is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The function accepts any `tx_hash: H256` and immediately dispatches `remove_local_tx` to the pool service. There is no check that:

- The caller submitted the transaction being removed.
- The caller has any cryptographic relationship to the transaction's inputs or outputs.
- The caller holds any privileged role.

The pool-side handler is equally unconditional:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    // ... removes from verify_queue, orphan pool, and main pool
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.remove_tx(&id)
}
``` [2](#0-1) 

`remove_tx` in turn calls `remove_entry_and_descendants`, which removes the target transaction **and every descendant** in one operation: [3](#0-2) 

The RPC is registered in the standard `Pool` module — not a debug or admin module — and is enabled by default: [4](#0-3) 

The RPC server binds to `127.0.0.1:8114` by default with **no authentication layer**: [5](#0-4) 

Any process on the same host — or any remote client when the operator exposes the port — can call this endpoint with an arbitrary hash.

---

### Impact Explanation

An attacker who can reach the RPC port can:

1. Enumerate all pending transactions via `get_raw_tx_pool` (also unauthenticated, same module).
2. Call `remove_transaction` with any observed `tx_hash`.
3. Silently evict the victim's transaction **and its entire descendant chain** from the pool.
4. Repeat indefinitely each time the victim resubmits, creating a sustained denial-of-service against transaction confirmation.

Because `remove_entry_and_descendants` is used, a single call targeting a parent transaction can cascade-remove an entire pre-signed transaction chain (e.g., a payment channel update sequence), amplifying the impact beyond a single transaction.

---

### Likelihood Explanation

- **Default localhost binding**: Any process running on the node host (co-located services, scripts, other users on a shared server) can reach port 8114 without credentials.
- **Network-exposed deployments**: The configuration explicitly supports binding to non-localhost addresses. Operators who expose the RPC for remote tooling (wallets, explorers, dApps) expose this endpoint to any remote caller.
- **Zero attacker cost**: The attack requires only knowing a transaction hash (publicly observable via `get_raw_tx_pool`) and issuing a single HTTP POST. No key material, no mining power, no special protocol knowledge.
- **No rate limiting or caller identity**: The RPC layer records no caller identity and imposes no per-caller limits on destructive pool operations.

---

### Recommendation

1. **Restrict `remove_transaction` to a privileged/admin module** (e.g., `Debug` or a new `Admin` module) that is disabled by default and requires explicit operator opt-in, consistent with how other destructive operations like `truncate` are handled.
2. **Add caller-identity gating**: Require the caller to provide a signature over the `tx_hash` using a key that corresponds to an input's lock script in the transaction, proving ownership before removal is permitted.
3. **At minimum, document the security boundary explicitly** in the RPC method's doc comment, warning that this endpoint must not be exposed to untrusted callers, and consider moving it out of the default-enabled `Pool` module.

---

### Proof of Concept

**Attacker-controlled entry path**: RPC caller (no privileges required).

**Steps**:

1. Alice broadcasts a high-value transaction via `send_transaction`. The node accepts it into the pending pool.
2. Attacker polls `get_raw_tx_pool` (also unauthenticated) and observes Alice's `tx_hash`.
3. Attacker sends:
   ```json
   {
     "id": 1,
     "jsonrpc": "2.0",
     "method": "remove_transaction",
     "params": ["<alice_tx_hash>"]
   }
   ```
   to `http://127.0.0.1:8114` (or the exposed RPC address).
4. The node calls `remove_local_tx` → `remove_tx` → `remove_entry_and_descendants`, evicting Alice's transaction and all descendants from the pool. The call returns `true`.
5. Alice's transaction is gone. Miners will never include it. Alice must resubmit.
6. Attacker repeats step 2–5 each time Alice resubmits, indefinitely blocking confirmation at zero cost.

The root cause — accepting an arbitrary `tx_hash` with no caller authorization — is the direct CKB analog of the `requestDepositWithPermit` pattern where `owner` is not restricted to `msg.sender`.

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

**File:** resource/ckb.toml (L177-180)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
```
