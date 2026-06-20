### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Arbitrary Transactions from the Pool - (File: rpc/src/module/pool.rs)

### Summary
The `remove_transaction` JSON-RPC method in the CKB Pool module accepts only a transaction hash and immediately evicts the matching transaction and all its descendants from the tx-pool. There is no ownership check, no caller identity verification, and no authentication middleware anywhere in the RPC stack. Any party that can reach the RPC port — including any local process, any dApp the node operator connects to, or any remote caller if the port is exposed — can silently remove transactions submitted by other users, directly analogous to `liquidateFrom` being `public` instead of `internal`.

### Finding Description

The `remove_transaction` RPC trait is declared in `rpc/src/module/pool.rs`:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

Its implementation contains no caller-identity or ownership check:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The call chain flows through `tx-pool/src/service.rs` (`remove_local_tx`) → `tx-pool/src/process.rs` (`remove_tx`) → `tx-pool/src/pool.rs` (`remove_tx`) → `tx-pool/src/component/pool_map.rs` (`remove_entry_and_descendants`). None of these layers record or verify who submitted the transaction being removed.

A grep for any authentication middleware (`BasicAuth`, `auth`, `middleware`, `require_auth`) across all of `rpc/src/` returns zero matches, confirming there is no RPC-level access control.

The Pool module is enabled by default in the production configuration:

```toml
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

The parallel to the Perpetual bug is exact: `liquidateFrom` was `public` instead of `internal`, so anyone could call it to force another account into a liquidated position. Here, `remove_transaction` is a public RPC endpoint with no ownership gate, so any caller can force another user's transaction out of the pool.

### Impact Explanation

Any caller with network access to the RPC port can:

1. **Targeted transaction censorship** — observe a victim's pending or proposed transaction hash (visible via `get_raw_tx_pool`), then call `remove_transaction` to evict it before it is committed. The victim's inputs remain locked until they resubmit.
2. **Cascade eviction** — `remove_entry_and_descendants` removes the target transaction *and all dependent descendants*, so a single call can evict an entire transaction chain built by the victim.
3. **Proposed-stage disruption** — a transaction in the `Proposed` state is inside the two-block confirmation window; evicting it at this stage forces the submitter to restart the proposal cycle, delaying finality.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, but:
- Many node operators expose the port for wallets, dApps, or block explorers.
- Any malicious local process (e.g., a compromised dependency, a malicious dApp running in a browser with CORS misconfiguration) can reach `localhost`.
- The attack requires only the target transaction hash, which is publicly observable from `get_raw_tx_pool` or network propagation.
- No key material, no privileged role, and no cryptographic capability is required.

### Recommendation

Add an ownership/authorization check before allowing removal. The simplest correct fix mirrors the Perpetual report's recommendation: restrict `remove_transaction` so it can only be called by the submitter of the transaction, or move it to a separate privileged/authenticated RPC module (analogous to making `liquidateFrom` `internal`). At minimum, the Pool module should require that the caller prove knowledge of the transaction's inputs (e.g., by signing the removal request with a key that controls one of the input cells), or the method should be moved out of the default-enabled `Pool` module into a restricted `Debug`/`IntegrationTest` module that is disabled in production.

### Proof of Concept

1. Node A submits a transaction: `send_transaction({...})` → receives `tx_hash = 0xabc...`.
2. Attacker (any other RPC caller) observes the hash via `get_raw_tx_pool`.
3. Attacker calls:
   ```json
   {"jsonrpc":"2.0","method":"remove_transaction","params":["0xabc..."],"id":1}
   ```
4. Response: `{"result": true}`.
5. Node A's transaction and all its descendants are gone from the pool. Node A must resubmit, and the cycle can be repeated indefinitely to permanently censor the transaction.

--- [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
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

**File:** tx-pool/src/component/pool_map.rs (L252-264)
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
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
