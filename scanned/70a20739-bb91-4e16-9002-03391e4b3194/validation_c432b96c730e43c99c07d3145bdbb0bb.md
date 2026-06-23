### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Any Caller to Evict Pending Transactions Without Authorization - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `Pool` RPC module exposes `remove_transaction` and `clear_tx_pool` as public JSON-RPC methods with no authentication or caller-identity check. Any client that can reach the RPC port can silently evict any pending transaction submitted by any other user, or wipe the entire mempool. This is a direct structural analog to the Primitive Finance `take` function: a state-mutating, externally callable function with no access controls that allows an unprivileged caller to interfere with other users' in-flight operations.

---

### Finding Description

**Root cause — `remove_transaction`:** [1](#0-0) 

The trait declaration exposes `remove_transaction` as a plain RPC method. The implementation: [2](#0-1) 

The implementation calls `tx_pool.remove_local_tx(tx_hash.into())` directly. There is no check on who the caller is, whether the caller submitted the transaction, or whether the caller has any relationship to the transaction being removed.

**Root cause — `clear_tx_pool`:** [3](#0-2) 

`clear_tx_pool` wipes the entire mempool in one call. Again, no authentication, no authorization, no caller identity check.

**Root cause — `clear_tx_verify_queue`:** [4](#0-3) 

Same pattern: clears the verification queue with no access control.

**Module is enabled by default.** The default production configuration includes `"Pool"` in the enabled modules list: [5](#0-4) 

**The RPC has no authentication layer.** The entire RPC service has no built-in authentication mechanism. The only protection is the bind address: [6](#0-5) 

The documentation itself acknowledges the risk but provides no mitigation within the code: [7](#0-6) 

**The underlying tx-pool service processes the removal unconditionally:** [8](#0-7) [9](#0-8) 

No submitter identity is stored or checked at any layer.

---

### Impact Explanation

An attacker who can reach the RPC port (any machine on the same network as a node that has exposed its RPC, or any process on the same host) can:

1. **Targeted transaction eviction**: Call `get_raw_tx_pool` to enumerate all pending transactions, identify a victim's high-value or time-sensitive transaction by hash, and call `remove_transaction` to silently drop it from the pool before it is proposed or committed. The victim's transaction disappears with no error or notification.

2. **Full mempool wipe**: Call `clear_tx_pool` to evict every pending transaction from every user simultaneously, causing a complete denial of transaction processing.

3. **Verification queue disruption**: Call `clear_tx_verify_queue` to drop all transactions currently being verified, stalling the pipeline.

Concrete harms:
- Transactions that are time-sensitive (e.g., those using `since`-locked inputs tied to epoch windows, or layer-2 challenge-response transactions) can be made to miss their valid commitment window by repeated eviction.
- Users who paid fees to third-party submission services lose those fees and must resubmit.
- A node operator running a public RPC endpoint (common for wallets and dApps) exposes all their users' pending transactions to targeted censorship by any client of that endpoint.

---

### Likelihood Explanation

Many production CKB nodes expose their RPC to non-localhost addresses to serve wallets, dApps, and block explorers. Any client of such a node — including a malicious dApp user or a script running on the same host — can call these methods. The attack requires only a single HTTP POST request with a known transaction hash (obtainable from `get_raw_tx_pool`). No key material, no PoW, no privileged access is needed.

---

### Recommendation

1. Introduce a per-method authorization tier. Destructive pool management methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`) should require an explicit opt-in token or be restricted to a separate, non-default administrative endpoint.
2. At minimum, add a configurable allowlist of IP addresses permitted to call mutating pool methods, separate from the read-only allowlist.
3. Document clearly in the RPC module that these methods are administrative and must not be exposed on public-facing endpoints.

---

### Proof of Concept

Attacker preconditions: RPC port is reachable (e.g., node operator has set `listen_address = "0.0.0.0:8114"`, which is common for public nodes).

**Step 1 — Enumerate the pool:**
```json
{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}
```
Response contains all pending transaction hashes.

**Step 2 — Evict a specific victim transaction:**
```json
{"jsonrpc":"2.0","method":"remove_transaction",
 "params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],"id":2}
```
Response: `{"result": true}` — transaction is gone from the pool with no authentication required.

**Step 3 — Wipe the entire pool:**
```json
{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":3}
```
Response: `{"result": null}` — all pending transactions evicted.

The implementation path is: HTTP POST → `PoolRpcImpl::remove_transaction` → `TxPoolController::remove_local_tx` → `TxPoolService::remove_tx` → pool entry deleted. No authorization check exists at any step. [2](#0-1) [10](#0-9) [9](#0-8)

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

**File:** rpc/src/module/pool.rs (L694-700)
```rust
    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
```

**File:** resource/ckb.toml (L177-187)
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
```

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** rpc/README.md (L4-6)
```markdown

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

```

**File:** tx-pool/src/service.rs (L272-275)
```rust
    /// Remove tx from tx-pool
    pub fn remove_local_tx(&self, tx_hash: Byte32) -> Result<bool, AnyError> {
        send_message!(self, RemoveLocalTx, tx_hash)
    }
```

**File:** tx-pool/src/service.rs (L826-834)
```rust
        Message::RemoveLocalTx(Request {
            responder,
            arguments: tx_hash,
        }) => {
            let result = service.remove_tx(tx_hash).await;
            if let Err(e) = responder.send(result) {
                error!("Responder sending remove_tx result failed {:?}", e);
            };
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
