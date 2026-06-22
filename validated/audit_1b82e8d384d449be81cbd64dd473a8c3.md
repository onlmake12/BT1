### Title
Any Local RPC Caller Can Remove Another User's Pending Transaction Without Ownership Check — (`rpc/src/module/pool.rs`)

### Summary

The `remove_transaction` RPC method in `rpc/src/module/pool.rs` removes any pending transaction from the tx pool by hash, with no check that the caller is the same party who submitted the transaction. Because the CKB tx pool records no submitter identity, any local RPC caller who can reach the endpoint can silently evict a transaction submitted by a different caller. This is a direct structural analog to the reported `cancelSignature` issue: a co-authorized party (any local RPC user) can cancel another party's pending operation (their submitted transaction) using only the operation's identifier (the tx hash).

### Finding Description

`remove_transaction` is implemented in `rpc/src/module/pool.rs` at line 662:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The only check performed is whether the tx hash exists in the pool. There is no record of who submitted the transaction, and no comparison between the caller's identity and the original submitter. The underlying `remove_tx` in `tx-pool/src/process.rs` likewise operates purely on the `ProposalShortId` derived from the hash:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    // removes from verify_queue, orphan pool, and tx_pool — no submitter check
    ...
    tx_pool.remove_tx(&id)
}
``` [2](#0-1) 

The CKB RPC server has no per-method or per-caller authentication. The `Pool` module (which includes `remove_transaction`) is enabled by default in the standard configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [3](#0-2) 

The RPC binds to `127.0.0.1:8114` by default, meaning any process running on the same host — including other local users, co-located services, or scripts — can reach it without any credential.

**Attack scenario:**

1. Alice submits a high-value transaction via `send_transaction` and receives its hash `0xABC...`.
2. Bob, a different local process or user with access to the same RPC endpoint, observes the hash (e.g., from a public mempool explorer, a shared log, or by calling `get_raw_tx_pool`).
3. Bob calls `remove_transaction("0xABC...")`.
4. Alice's transaction is silently evicted from the pool. It will not be confirmed unless Alice resubmits it — and Bob can repeat the eviction indefinitely.

### Impact Explanation

A malicious local RPC caller can permanently prevent any specific pending transaction from being confirmed by repeatedly removing it from the pool. This enables:

- **Targeted transaction censorship**: an attacker can block a specific user's transaction from ever being mined.
- **Front-running facilitation**: the attacker removes a competing transaction and submits their own in its place.
- **Denial of service against specific users**: in shared-node environments (hosted nodes, development clusters, multi-tenant services), one tenant can disrupt all others.

The impact is **medium**: it does not directly steal funds, but it can permanently block a user's transaction from being confirmed, which in time-sensitive scenarios (e.g., DAO withdrawals, time-locked cells) can cause irreversible loss of opportunity or funds.

### Likelihood Explanation

The likelihood is **medium**. The RPC is localhost-only by default, which limits the attacker to processes on the same host. However:

- Shared CKB node deployments (hosted services, development environments, CI pipelines) are common and explicitly supported.
- The `get_raw_tx_pool` RPC (also in the `Pool` module, no auth) lets any caller enumerate all pending tx hashes, making target discovery trivial.
- No special privilege is required beyond reaching the RPC port — the attacker profile is "supported local CLI/RPC user," which is explicitly in scope.

### Recommendation

Track the submitter identity (e.g., source IP or a caller token) when a transaction is added to the pool via `send_transaction`. In `remove_transaction`, verify that the caller matches the original submitter, or restrict `remove_transaction` to a separate privileged RPC module (e.g., `Admin`) that is not enabled by default alongside `Pool`. At minimum, document that `remove_transaction` is an administrative operation that must not be co-exposed with untrusted `send_transaction` callers.

### Proof of Concept

```bash
# Step 1: Alice submits a transaction and gets its hash
TX_HASH=$(curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<alice_tx>, "passthrough"],"id":1}' \
  | jq -r '.result')

# Step 2: Bob (any other local process) enumerates the pool to find Alice's tx
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":2}'

# Step 3: Bob removes Alice's transaction — no credential required
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d "{\"jsonrpc\":\"2.0\",\"method\":\"remove_transaction\",\"params\":[\"$TX_HASH\"],\"id\":3}"
# Returns: {"result": true}

# Alice's transaction is now gone from the pool.
# Bob can repeat this every time Alice resubmits.
```

Root cause: `remove_transaction` in `rpc/src/module/pool.rs` performs no submitter-identity check before calling `tx_pool.remove_local_tx`, and the tx pool (`tx-pool/src/process.rs`) stores no submitter metadata to check against. [4](#0-3) [2](#0-1)

### Citations

**File:** rpc/src/module/pool.rs (L220-255)
```rust
    /// Removes a transaction and all transactions which depends on it from tx pool if it exists.
    ///
    /// ## Params
    ///
    /// * `tx_hash` - Hash of a transaction.
    ///
    /// ## Returns
    ///
    /// If the transaction exists, return true; otherwise, return false.
    ///
    /// ## Examples
    ///
    /// Request
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "method": "remove_transaction",
    ///   "params": [
    ///     "0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"
    ///   ]
    /// }
    /// ```
    ///
    /// Response
    ///
    /// ```json
    /// {
    ///   "id": 42,
    ///   "jsonrpc": "2.0",
    ///   "result": true
    /// }
    /// ```
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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
