### Title
Missing Access Control on `remove_transaction` RPC Allows Any Caller to Evict Arbitrary Pending Transactions from the Pool — (`rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in `PoolRpcImpl` performs a destructive, privileged state mutation — evicting any named transaction and all its dependents from the tx-pool — without any caller-identity check, ownership verification, or role guard. Any entity with RPC access (explicitly listed as an in-scope attacker: "RPC caller" / "supported local CLI/RPC user") can silently censor any other user's pending transaction by supplying its hash. The RPC server itself has no authentication middleware, so the absence of a per-method guard is the only protection layer, and it does not exist.

---

### Finding Description

**Root cause — no access control on the handler**

`PoolRpcImpl::remove_transaction` in `rpc/src/module/pool.rs` (lines 662–669) directly forwards the caller-supplied hash to `tx_pool_controller().remove_local_tx()` with zero identity or ownership checks:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The message is dispatched to the tx-pool service, which calls `service.remove_tx(tx_hash)` — a function that unconditionally removes the transaction from the verify queue, orphan pool, and main pool map: [2](#0-1) [3](#0-2) 

**Root cause — no authentication on the RPC server**

The RPC server (`rpc/src/server.rs`) builds its Axum router with `CorsLayer::permissive()` and no authentication middleware. Every incoming POST is dispatched directly to `io.handle_call(call, T::default())`: [4](#0-3) [5](#0-4) 

There is no bearer token, Basic-auth check, or per-method role guard anywhere in the server pipeline. The miner client has a `parse_authorization` helper that can *send* Basic-auth headers, but the server never validates them.

**The `Pool` module is enabled by default**, so `remove_transaction` is reachable on every standard node: [6](#0-5) 

The same absence of access control applies to `clear_tx_pool` (wipes the entire pool) and `clear_tx_verify_queue` (wipes the verification queue): [7](#0-6) 

---

### Impact Explanation

An unprivileged RPC caller can:

1. **Targeted transaction censorship**: Supply any known `tx_hash` to `remove_transaction` and evict that transaction — plus every descendant that depends on it — from the pool. The victim's transaction is silently dropped and must be resubmitted. An attacker who monitors the mempool can loop this call to permanently prevent a specific transaction from ever being confirmed.

2. **Full pool wipe**: Call `clear_tx_pool()` to atomically discard every pending and proposed transaction on the node, resetting the block assembler state. All in-flight user transactions are lost.

3. **Verify-queue drain**: Call `clear_tx_verify_queue()` to drop all transactions currently awaiting script verification, stalling throughput.

The `remove_tx` path removes from the verify queue, orphan pool, and main pool map in sequence, so no transaction state survives the call: [3](#0-2) 

---

### Likelihood Explanation

- The RPC binds to `127.0.0.1:8114` by default, so the attacker needs local RPC access. The scope explicitly lists **"RPC caller"** and **"supported local CLI/RPC user"** as valid unprivileged attacker profiles.
- Many node operators expose the RPC to a local network for dApp backends or wallet services; in those deployments any process on the LAN qualifies.
- The `CorsLayer::permissive()` setting means a malicious web page loaded in a browser on the same machine can issue cross-origin POST requests to `http://127.0.0.1:8114` and invoke `remove_transaction` or `clear_tx_pool` without any preflight rejection.
- No credential, key, or privileged role is required — only the ability to send an HTTP POST. [8](#0-7) 

---

### Recommendation

Add a caller-identity or role guard at the RPC layer. Concretely:

1. **Per-method access tiers**: Classify RPC methods into read-only (safe for any caller) and write/destructive (require an operator token). `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` must be in the restricted tier.
2. **Server-side authentication middleware**: Implement a Tower/Axum middleware layer that validates a configurable bearer token or Basic-auth credential before dispatching any destructive method. The miner client already has the client-side plumbing (`parse_authorization`); the server side is missing the matching check.
3. **Ownership check on `remove_transaction`**: At minimum, verify that the submitter of the removal request is the same peer/source that originally submitted the transaction (tracked via `submit_local_tx` origin tagging).

---

### Proof of Concept

With a default CKB node running (`rpc.listen_address = "127.0.0.1:8114"`, `modules` includes `"Pool"`):

**Step 1 — victim submits a transaction:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<victim_tx_json>,"passthrough"]}'
# returns: {"result":"0xABCD..."}
```

**Step 2 — attacker (any local process, no credentials) removes it:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"remove_transaction","params":["0xABCD..."]}'
# returns: {"result":true}
```

The victim's transaction is now gone from the pool. `tx_pool_info` will show `pending` decremented. The victim receives no notification. Repeating step 2 after every resubmission achieves permanent censorship of that transaction.

**Full-pool wipe variant:**
```bash
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# returns: {"result":null}
```

All pending transactions on the node are discarded with a single unauthenticated call. [9](#0-8) [10](#0-9)

### Citations

**File:** rpc/src/module/pool.rs (L254-255)
```rust
    #[rpc(name = "remove_transaction")]
    fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;
```

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
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

**File:** rpc/src/module/pool.rs (L684-701)
```rust
    fn clear_tx_pool(&self) -> Result<()> {
        let snapshot = Arc::clone(&self.shared.snapshot());
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_pool(snapshot)
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
    }

    fn clear_tx_verify_queue(&self) -> Result<()> {
        let tx_pool = self.shared.tx_pool_controller();
        tx_pool
            .clear_verify_queue()
            .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;

        Ok(())
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

**File:** rpc/src/server.rs (L256-259)
```rust
        Ok(request) => match request {
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;

```

**File:** resource/ckb.toml (L190-193)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```
