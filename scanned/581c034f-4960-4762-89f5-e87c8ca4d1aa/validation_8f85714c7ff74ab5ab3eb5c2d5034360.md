### Title
Unauthorized Transaction Removal via Unauthenticated `remove_transaction` RPC — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in CKB's Pool module accepts only a transaction hash and performs no signature verification or ownership check. Any caller with access to the RPC endpoint can remove any pending transaction from the tx-pool without proving they are the transaction's owner. This is a direct analog to the external report's finding: just as `cancelLimitOrder` failed to verify the trader's signature before invalidating a nonce, `remove_transaction` fails to verify the caller's identity before evicting a transaction.

---

### Finding Description

The `remove_transaction` RPC method is defined in `rpc/src/module/pool.rs`:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The only parameter is `tx_hash: H256`. There is no:
- Signature from the transaction's input cell lock-script owners
- Proof of ownership of any input cell
- Any form of caller identity verification

The call chain is:
1. `remove_transaction(tx_hash)` → `tx_pool_controller.remove_local_tx(tx_hash)` → `TxPoolService::remove_tx(tx_hash)` → `pool_map.remove_entry_and_descendants(id)`

The `remove_tx` implementation in `tx-pool/src/process.rs` removes the transaction from the verify queue, orphan pool, and main pool unconditionally:

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

The Pool RPC module is enabled by default in the standard node configuration, and the RPC server binds to `127.0.0.1:8114` by default. Any process running on the same host — including malicious scripts, compromised co-located services, or any unprivileged local user — can call this endpoint with no authentication token required.

---

### Impact Explanation

An attacker with access to the RPC endpoint (any local process on the node host, or any remote caller if the operator has exposed the RPC beyond localhost) can:

1. Observe the tx-pool via `get_raw_tx_pool` to enumerate pending transaction hashes.
2. Call `remove_transaction` with any target `tx_hash`.
3. The target transaction — along with all its descendants — is permanently evicted from the pool without the transaction owner's consent.
4. The attacker can repeat this every time the victim resubmits, creating a sustained censorship loop.

This prevents the victim's transaction from ever being proposed or committed to a block, effectively freezing their funds in a live-liveness denial. Unlike a simple network-level drop, this is a node-state mutation: the transaction is actively removed from the pool and recorded as rejected, which may affect downstream tooling that tracks pool status.

---

### Likelihood Explanation

The Pool module is enabled by default in the production configuration. The RPC binds to `127.0.0.1:8114` by default, which means any process on the same machine — including unprivileged user-space processes — can reach it. There is no API token, no HTTP authentication, and no per-method access control within the Pool module. The `remove_transaction` method is documented publicly and its behavior is straightforward to exploit. Any co-located service, malicious dependency, or local attacker can trivially enumerate pool contents and remove targeted transactions.

---

### Recommendation

Add caller authorization to `remove_transaction`. The caller should be required to provide a valid signature over the transaction hash using a key that corresponds to at least one input cell's lock script in the target transaction. This mirrors the correct pattern described in the external report: the cancellation/removal action must be cryptographically bound to the owner of the affected state.

Alternatively, restrict `remove_transaction` (and `clear_tx_pool`, `clear_tx_verify_queue`) to a separate privileged RPC module that is disabled by default and requires explicit operator opt-in, similar to the `Debug` or `IntegrationTest` modules.

---

### Proof of Concept

**Attacker preconditions**: Any process on the same host as the CKB node (no keys, no privileges required).

**Steps**:

```bash
# Step 1: Enumerate pending transactions
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'
# Returns: {"result":{"pending":["0xVICTIM_TX_HASH",...],...}}

# Step 2: Remove the victim's transaction — no signature required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xVICTIM_TX_HASH"],"id":2}'
# Returns: {"result":true}
```

The victim's transaction and all its descendants are evicted. The attacker can loop this indefinitely to prevent confirmation.

---

**Root cause references**: [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** resource/ckb.toml (L182-193)
```text
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
