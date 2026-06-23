### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Arbitrary Transactions from the Pool — (`File: rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` JSON-RPC method in the `Pool` module accepts only a transaction hash and immediately removes that transaction — plus every descendant — from the tx-pool. No check is performed to verify that the caller is the original submitter of the transaction. Any local process (or any remote caller when the operator exposes the RPC port) can silently drain the entire pending pool by iterating over known or observable transaction hashes.

---

### Finding Description

`PoolRpc::remove_transaction` is defined as:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

Its implementation is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The only parameter is `tx_hash`. There is no caller identity, no signature, no proof of ownership, and no comparison against who originally submitted the transaction. The call is forwarded directly to `TxPoolController::remove_local_tx`, which calls `process.rs::remove_tx`:

```rust
pub(crate) async fn remove_tx(&self, tx_hash: Byte32) -> bool {
    let id = ProposalShortId::from_tx_hash(&tx_hash);
    // removes from verify_queue, orphan pool, and main pool
    ...
    let mut tx_pool = self.tx_pool.write().await;
    tx_pool.remove_tx(&id)
}
``` [2](#0-1) 

`remove_tx` in the pool itself calls `remove_entry_and_descendants`, which recursively removes the target transaction **and all transactions that depend on it**: [3](#0-2) 

The `Pool` module — which contains `remove_transaction` — is **enabled by default** in the shipped configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [4](#0-3) 

The RPC server binds to `127.0.0.1:8114` by default, but the documentation explicitly acknowledges that operators change this to serve external clients, and no IP-based access control or authentication layer exists in the RPC server itself. [5](#0-4) 

---

### Impact Explanation

An attacker who can reach the RPC port (any local process by default; any remote caller when the operator exposes the port, which is a documented and common deployment pattern) can:

1. Enumerate pending transaction hashes via `get_raw_tx_pool` (also in the `Pool` module, no auth).
2. Call `remove_transaction` for each hash.
3. Silently evict every pending and proposed transaction — including entire dependency chains — from the pool.

This is a complete **tx-pool DoS**: legitimate users' transactions are dropped without confirmation, fees are wasted, and time-sensitive transactions (e.g., DAO withdrawals with lock expiry, RBF replacements) may miss their windows. The attacker pays nothing and leaves no on-chain trace.

**Impact: High** — complete erasure of the mempool state with no cost to the attacker.

---

### Likelihood Explanation

- The `Pool` module is **on by default**; no operator action is required to expose the method.
- `get_raw_tx_pool` (same module, same zero-auth) provides the full list of hashes to target.
- Any co-located process (scripts, indexers, wallets, monitoring agents sharing the same host) is a valid attacker with zero privileges.
- Operators who expose the RPC port for dApps or block explorers — a common and documented pattern — extend the attack surface to the entire internet.

**Likelihood: High** — the precondition is simply being able to reach port 8114, which is true for every local process by default.

---

### Recommendation

Add an ownership/submitter check before allowing removal. Two complementary mitigations:

1. **Track the submitter**: When a transaction is admitted via `submit_local_tx`, record the source identity (e.g., a per-session token or the submitter's IP/connection ID) alongside the pool entry. In `remove_transaction`, verify the caller's identity matches the recorded submitter before proceeding.

2. **Separate the method into a privileged module**: Move `remove_transaction` (and `clear_tx_pool`, `clear_tx_verify_queue`) out of the default `Pool` module into a new `Admin` or `Debug` module that is **disabled by default**, analogous to how `IntegrationTest` and `Debug` are off by default. This prevents accidental exposure.

---

### Proof of Concept

```bash
# Step 1: submit a transaction (as the legitimate owner)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<tx_json>,"passthrough"]}'
# -> returns tx_hash

# Step 2: as an anonymous attacker, list all pool hashes
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# -> returns list of all pending tx hashes

# Step 3: remove the victim's transaction (no auth required)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"remove_transaction","params":["<tx_hash>"]}'
# -> {"result": true}  -- transaction and all descendants evicted
```

The attacker supplies only the hash — no key, no signature, no proof of ownership — and the transaction is permanently removed from the pool.

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

**File:** resource/ckb.toml (L178-183)
```text
# By default RPC only binds to localhost, thus it only allows accessing from the same machine.
#
# Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged.
# Please strictly limit the access to only trusted machines.
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
```

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```
