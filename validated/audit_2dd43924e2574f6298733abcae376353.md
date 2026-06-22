### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Arbitrary Pending Transactions — (`rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` JSON-RPC method in CKB's Pool module performs no ownership or authorization check. Any caller who can reach the RPC endpoint can permanently evict any pending, proposed, or orphan transaction — and its entire descendant chain — from the tx-pool. This is a direct structural analog to M-13: just as the Arbitrum `callValueRefundAddress` gave an attacker-controlled address the unilateral right to cancel a retryable ticket, the CKB `remove_transaction` endpoint gives any RPC caller the unilateral right to cancel any in-flight transaction.

### Finding Description

`remove_transaction` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl` with no access control whatsoever:

```rust
// rpc/src/module/pool.rs  line 254-255
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

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

The underlying `remove_tx` in the service layer removes the target transaction **and all its descendants** from every pool stage (verify queue, orphan pool, pending, proposed):

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

And `remove_tx` on the pool itself removes the entry and all descendants atomically:

```rust
// tx-pool/src/pool.rs  line 358-361
pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
    let entries = self.pool_map.remove_entry_and_descendants(id);
    !entries.is_empty()
}
```

There is no check that the caller is the original submitter of the transaction, no IP allowlist enforcement at the method level, and no capability token. The method is part of the standard `Pool` RPC module, which is enabled by default and documented publicly in `rpc/README.md` (line 4707–4746).

### Impact Explanation

An attacker who can reach the RPC port (publicly exposed nodes are common for dApp infrastructure) can:

1. Monitor the mempool via `get_raw_tx_pool`.
2. Call `remove_transaction(<victim_tx_hash>)` for any pending or proposed transaction.
3. The transaction and its entire descendant chain are permanently evicted — they are not re-broadcast, not re-queued, and not recorded in `recent_reject` (since `remove_tx` bypasses the reject pipeline).
4. The victim must re-sign and re-submit. For time-locked transactions (`since` field), the window may have closed. For DeFi transactions (e.g., liquidations, arbitrage), the opportunity is lost.
5. The attack is free (no gas/fee cost), repeatable at will, and requires no cryptographic material.

### Likelihood Explanation

Many CKB node operators expose the RPC port publicly (e.g., `rpc.listen_address = "0.0.0.0:8114"`) to serve wallets and dApps. The Pool module is enabled by default. The attacker needs only HTTP access to the RPC port and knowledge of a target transaction hash (trivially obtained from `get_raw_tx_pool` or network relay observation). No privileged role, key, or majority hashpower is required.

### Recommendation

Restrict `remove_transaction` to callers that can prove ownership of the transaction being removed. The minimal fix is to verify that at least one input of the target transaction was previously submitted by the same local caller (e.g., via a session token or by restricting the method to the `127.0.0.1` loopback interface only, enforced at the method level rather than relying solely on operator configuration). Alternatively, move `remove_transaction` into a separate admin-only RPC module (analogous to `IntegrationTest` or `Miner`) that is disabled by default and documented as requiring trusted-network access.

### Proof of Concept

1. Alice submits a time-sensitive transaction `tx_A` (e.g., a DAO withdrawal with a `since` lock expiring in 2 blocks) via `send_transaction`. It enters the pending pool.
2. Bob calls `get_raw_tx_pool` and observes `tx_A`'s hash.
3. Bob calls `remove_transaction("0x<tx_A_hash>")`. The RPC returns `true`.
4. `tx_A` and all its descendants are evicted from the pool with no record in `recent_reject`.
5. Alice's transaction is not included in the next block. By the time she re-submits, the `since` lock window has passed and the transaction is permanently invalid.
6. Bob repeats for every new transaction Alice submits.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
