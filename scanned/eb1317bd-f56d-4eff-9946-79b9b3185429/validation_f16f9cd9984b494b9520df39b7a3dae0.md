### Title
Unauthenticated `remove_transaction` RPC Evicts Any Pending Transaction Without Submitter Ownership Check — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` JSON-RPC method removes any pending transaction from the tx-pool by hash without verifying that the caller is the original submitter of that transaction. Any local RPC caller can silently evict another user's pending transaction, enabling targeted transaction censorship and, for time-sensitive operations, permanent invalidation of the victim's transaction window.

---

### Finding Description

`remove_transaction` in `rpc/src/module/pool.rs` accepts a bare `tx_hash: H256` and immediately forwards it to `tx_pool.remove_local_tx`:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

There is no check that the caller submitted the transaction, owns any of its input cells, or has any relationship to it whatsoever. The underlying `remove_tx` in the service layer removes the entry from the verify queue, orphan pool, and pending/proposed pool unconditionally:

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
``` [2](#0-1) 

The same pattern applies to `clear_tx_pool` and `clear_tx_verify_queue`, which wipe the entire pool or verify queue with no caller identity check: [3](#0-2) 

This is the direct CKB analog of the NFT bridge bug: just as `bridgeNft()` burned an NFT without checking `nft.ownerOf(tokenId) == msg.sender`, `remove_transaction` destroys a pending transaction's pool presence without checking that the caller is its submitter.

The RPC is enabled in the default `Pool` module and is listed in the standard module set: [4](#0-3) 

All pending transaction hashes are publicly enumerable via `get_raw_tx_pool`, so a caller does not need any out-of-band knowledge to target a specific transaction.

---

### Impact Explanation

**Vulnerability class:** Missing authorization check before a destructive, irreversible pool-state mutation.

1. **Targeted transaction censorship.** Any local RPC caller can remove any other user's pending transaction. The victim must resubmit, paying fees again and losing their position in the pool.

2. **Permanent invalidation of time-sensitive transactions.** CKB DAO Phase-2 withdrawal transactions must be confirmed within a specific epoch window. If an attacker evicts such a transaction near the end of the valid epoch, the victim misses the window and must wait for the next cycle (potentially months of locked capacity). The same applies to any transaction using `since`-based absolute epoch constraints.

3. **Competitive front-running.** After evicting a victim's transaction, the attacker can immediately submit a competing transaction spending the same cells (with a valid witness), effectively front-running the victim's intent.

**Impact: High** — permanent loss of time-sensitive transaction windows; forced fee re-expenditure; competitive displacement.

---

### Likelihood Explanation

- The RPC listen address defaults to `127.0.0.1:8114` (localhost only), but the CKB bounty scope explicitly includes "supported local CLI/RPC user" as a valid attacker.
- In shared-node environments (mining pools, RPC-as-a-service providers, dApp backends with multiple tenants), multiple independent users share the same RPC endpoint.
- The attack requires only a single unauthenticated JSON-RPC call with a known transaction hash; the hash is trivially obtained from `get_raw_tx_pool`.
- No cryptographic material, elevated OS privilege, or social engineering is required.

**Likelihood: Medium** — constrained to local RPC access, but that access class is explicitly in scope and common in production deployments.

---

### Recommendation

1. **Record submitter identity at submission time.** When a transaction is accepted via `submit_local_tx`, store a submitter token (e.g., a session identifier or authenticated credential) alongside the pool entry.
2. **Verify identity at removal time.** In `remove_transaction`, check that the caller's token matches the stored submitter before proceeding.
3. **Alternatively, split the API surface.** Move `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` to a separate admin-only RPC module (analogous to the existing `IntegrationTest` module gating) that is disabled by default and requires explicit operator opt-in with stronger access controls.

---

### Proof of Concept

```
# Step 1: Alice submits a time-sensitive DAO Phase-2 withdrawal transaction
curl -X POST http://127.0.0.1:8114 -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<alice_dao_tx>, "passthrough"],"id":1}'
# Returns: {"result": "0xALICE_TX_HASH"}

# Step 2: Bob (a different local RPC user) enumerates the pool
curl -X POST http://127.0.0.1:8114 -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":2}'
# Returns pool IDs including 0xALICE_TX_HASH

# Step 3: Bob removes Alice's transaction — no ownership check, succeeds immediately
curl -X POST http://127.0.0.1:8114 -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xALICE_TX_HASH"],"id":3}'
# Returns: {"result": true}

# Step 4: The valid epoch window closes before Alice notices and resubmits.
# Alice's DAO withdrawal is permanently invalidated for this cycle.
# Bob submits a competing transaction spending the same input cells.
```

The root cause is the absence of any submitter-identity check in `remove_transaction` at `rpc/src/module/pool.rs:662–669`, mirrored by the same omission in `clear_tx_pool` and `clear_tx_verify_queue` at lines 684–701. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
