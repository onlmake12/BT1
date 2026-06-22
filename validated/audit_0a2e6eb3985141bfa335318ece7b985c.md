### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Evict Any User's Pending Transaction and All Descendants — (`File: rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` JSON-RPC method accepts only a transaction hash and performs no ownership or authorization check. Any caller who can reach the RPC endpoint can silently evict any other user's pending transaction — along with its entire descendant chain — from the tx-pool. For transactions that carry a time-sensitive `since` constraint (epoch, block, or timestamp), eviction before commitment can cause the user to miss their valid spending window, resulting in temporary or permanent inaccessibility of funds.

---

### Finding Description

`remove_transaction` is declared in `rpc/src/module/pool.rs` and is part of the default-enabled `Pool` module:

```rust
// rpc/src/module/pool.rs  line 254-255
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

The implementation contains no caller identity check, no signature verification, and no ownership proof:

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

`remove_local_tx` dispatches `Message::RemoveLocalTx` to the tx-pool service, which calls `service.remove_tx(tx_hash).await`:

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

`TxPool::remove_tx` calls `remove_entry_and_descendants`, which removes the target transaction **and every transaction that depends on it**:

```rust
// tx-pool/src/pool.rs  line 358-361
pub(crate) fn remove_tx(&mut self, id: &ProposalShortId) -> bool {
    let entries = self.pool_map.remove_entry_and_descendants(id);
    !entries.is_empty()
}
```

The `Pool` module is listed in the default-enabled modules in `resource/ckb.toml`:

```toml
# resource/ckb.toml  line 190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

The RPC server itself has no authentication layer — no API key, no token, no IP-based caller identity:

```rust
// rpc/src/server.rs  line 119-129
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())
    ...
```

---

### Impact Explanation

**Griefing — no profit motive, damage to users:**

An attacker who can reach the RPC endpoint (default `127.0.0.1:8114`) can call:

```json
{"method": "remove_transaction", "params": ["<victim_tx_hash>"]}
```

This immediately and silently removes the victim's transaction from the pending pool, the verify queue, and the orphan pool, along with every descendant transaction. The victim receives no notification.

**Permanent / temporary freezing of funds via `since` time-lock:**

CKB transactions carry a `since` field on each input that enforces absolute or relative time constraints (block number, epoch, or timestamp). The `SinceVerifier` enforces these at commit time. If a user submits a transaction that is only valid within a specific epoch window (e.g., a Nervos DAO withdrawal phase-2 claim, or a custom time-locked cell), and an attacker repeatedly evicts it before it can be committed, the user misses the valid window. For absolute `since` constraints, the window may never reopen, permanently locking the funds.

**Cascade removal of dependent transactions:**

Because `remove_entry_and_descendants` is called, a single `remove_transaction` call on a parent transaction removes the entire descendant chain. A user who has built a chain of 25 dependent transactions (the `max_ancestors_count` limit) loses all of them in one RPC call.

---

### Likelihood Explanation

- The `Pool` module is enabled by default. Any process on the same host — or any client if the operator has exposed the RPC beyond localhost — can call `remove_transaction` with no credentials.
- The attacker only needs to know the transaction hash, which is publicly observable via `get_raw_tx_pool` (also unauthenticated) or by watching the P2P relay network.
- The attack is trivially scriptable: poll `get_raw_tx_pool`, identify victim transactions, call `remove_transaction` in a loop. The victim cannot distinguish griefing from normal pool eviction.
- In shared-node deployments (e.g., a service provider running one CKB node for multiple users), all users share the same RPC endpoint, making cross-user griefing directly reachable.

---

### Recommendation

1. **Add caller-identity gating**: Introduce an optional RPC authentication token (e.g., bearer token in HTTP header) and require it for all state-mutating Pool methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`).
2. **Restrict destructive methods to a separate privileged module**: Move `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` out of the default `Pool` module into a separate `PoolAdmin` module that is disabled by default and requires explicit opt-in.
3. **Short-term mitigation**: Document clearly that `remove_transaction` is an operator-only method and should never be exposed on a shared or public endpoint.

---

### Proof of Concept

**Attacker-controlled entry path:** Any process reachable to the RPC endpoint (default localhost, or any IP if misconfigured).

**Step-by-step scenario:**

1. Alice submits a time-sensitive transaction (e.g., spending a cell with an absolute epoch `since` constraint valid only in epoch N):

```json
{"method": "send_transaction", "params": [<alice_tx>]}
```

2. Attacker polls the pool to discover Alice's transaction hash:

```json
{"method": "get_raw_tx_pool", "params": [false]}
```

3. Attacker calls `remove_transaction` before the transaction is proposed/committed:

```json
{"method": "remove_transaction", "params": ["<alice_tx_hash>"]}
// Returns: {"result": true}
```

4. Alice's transaction and all its descendants are silently removed from the pool. The `remove_tx` path in `tx-pool/src/process.rs` (line 440–455) clears it from the verify queue, orphan pool, and main pool with no callback to Alice.

5. If epoch N passes before Alice resubmits, her `since`-constrained transaction is now permanently invalid. Her funds remain locked until (if ever) another valid spending path exists.

**Relevant code locations:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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
