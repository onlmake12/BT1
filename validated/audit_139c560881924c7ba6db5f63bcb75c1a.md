### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Another User's Pending Transaction - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in CKB's Pool module contains no authorization or ownership check. Any RPC caller can supply an arbitrary `tx_hash` and permanently evict another user's pending transaction — along with all its descendants — from the transaction pool. This is a direct structural analog to H-01: a state-modifying function that accepts an attacker-controlled identifier for a victim's asset and updates that asset's state without verifying the caller is the rightful owner.

---

### Finding Description

The `remove_transaction` RPC is declared in the `PoolRpc` trait:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

Its implementation in `PoolRpcImpl` is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The call chain continues into `TxPoolService::remove_tx`:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    // removes from verify_queue, orphan pool, and main pool
    ...
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.remove_tx(&id)
}
```

At no point in this chain is there any check that:
- The caller submitted the transaction being removed.
- The caller has any relationship to the transaction's inputs or outputs.
- The caller holds any credential authorizing pool management.

The transaction hash of any pending transaction is publicly discoverable by any caller via `get_raw_tx_pool`. An attacker therefore has a complete, unauthenticated, targeted eviction primitive over the entire mempool.

---

### Impact Explanation

**Direct impact — targeted transaction eviction:** Any RPC caller can remove any other user's pending transaction and all its descendants from the pool. The victim must resubmit, paying at least the minimum fee rate again.

**Elevated impact — time-locked transactions:** CKB transactions support a `since` field encoding epoch-number, block-number, or timestamp-based time locks. DAO withdrawal (phase 2) transactions must be committed in a specific epoch window. If an attacker evicts a victim's DAO withdrawal transaction just before the epoch boundary closes, the victim misses that withdrawal window entirely and must wait for the next eligible epoch — a delay that can span weeks or months, representing a real loss of interest accrual and delayed access to funds.

**Cascade impact — descendant eviction:** The RPC removes the target transaction *and all its descendants*. A single call can evict an entire chain of dependent transactions belonging to multiple users, amplifying the damage beyond the single targeted hash.

**Systemic impact:** An attacker who continuously monitors `get_raw_tx_pool` and calls `remove_transaction` on every new entry can effectively prevent any transaction from being confirmed, constituting a targeted mempool censorship attack against specific addresses or all users.

---

### Likelihood Explanation

- The `remove_transaction` RPC is in the standard `Pool` module, enabled by default, with no authentication layer in the code.
- The attacker entry path requires only RPC access — a "supported local CLI/RPC user" or any remote caller if the operator exposes the RPC endpoint (common for dApp backends and mining pools).
- The victim's transaction hash is publicly readable via `get_raw_tx_pool` with no authentication.
- The attack requires a single JSON-RPC call with one known parameter. No brute force, no Sybil attack, no hashpower.

---

### Recommendation

1. **Restrict `remove_transaction` to the node operator only** by moving it to an admin-only RPC module (similar to how `clear_tx_pool` and `clear_tx_verify_queue` should be treated), or gate it behind a configurable access-control list.
2. **Add an ownership check**: before removing a transaction, verify that the caller's identity (e.g., a configured API key or local-only binding) matches the submitter recorded at submission time.
3. At minimum, **document clearly** that `remove_transaction` is an operator-only tool and should never be exposed on a public-facing RPC endpoint, and enforce this in the default configuration.

---

### Proof of Concept

1. **User A** submits a DAO withdrawal (phase 2) transaction `tx_victim` with a `since` field requiring commitment in epoch N. The transaction enters the pending pool.
2. **Attacker** calls `get_raw_tx_pool` (no authentication required) and observes `tx_victim`'s hash.
3. **Attacker** calls:
   ```json
   {
     "jsonrpc": "2.0",
     "method": "remove_transaction",
     "params": ["<tx_victim_hash>"],
     "id": 1
   }
   ```
4. `PoolRpcImpl::remove_transaction` calls `tx_pool.remove_local_tx(tx_victim_hash)` with no ownership check. The transaction and all its descendants are evicted.
5. **User A's** DAO withdrawal is gone from the pool. Epoch N closes before User A can resubmit and have the transaction proposed and committed. User A must wait for epoch N+k (the next eligible unlock epoch), losing weeks of interest and delayed access to their CKB.
6. The attacker repeats this for every new transaction User A submits, achieving indefinite censorship of a specific address at zero cost.

---

**Root cause files:** [1](#0-0) [2](#0-1) [3](#0-2)

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
