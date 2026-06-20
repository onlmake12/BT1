### Title
Any RPC Caller Can Remove Another User's Pending Transaction Without Ownership Verification — (`rpc/src/module/pool.rs`)

### Summary
`PoolRpcImpl::remove_transaction` in `rpc/src/module/pool.rs` accepts any `tx_hash` and unconditionally removes the matching transaction (and all its descendants) from the tx pool. There is no check that the caller is the original submitter of the transaction. Any process or user with access to the RPC port can silently evict any other user's pending transaction.

### Finding Description

The implementation of `remove_transaction` is:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [1](#0-0) 

The only parameter is `tx_hash`. There is no caller identity, no signature, no session token, and no check that the caller submitted the transaction. The method is part of the `Pool` module, which is enabled by default in the standard node configuration: [2](#0-1) 

The RPC server itself has no authentication middleware — it uses `CorsLayer::permissive()` and binds to a plain TCP socket with no credential layer: [3](#0-2) 

The underlying `remove_local_tx` call dispatches a `RemoveLocalTx` message that removes the transaction and all its descendants from the pending pool, orphan pool, and verify queue: [4](#0-3) [5](#0-4) 

### Impact Explanation

Any caller with access to the RPC port can remove any pending transaction submitted by any other user or service. In a shared-node deployment (exchange backend, dApp service, mining pool), multiple internal services share the same RPC endpoint. A malicious or compromised service can:

1. Enumerate pending transactions via `get_raw_tx_pool`.
2. Call `remove_transaction` on any of them.
3. The victim's transaction is silently dropped; it will never be confirmed unless resubmitted.

For time-sensitive transactions (e.g., DAO withdrawals with epoch-locked `since` fields, RBF replacements, or fee-bumping chains), forced removal causes direct financial harm. The `clear_tx_pool` method on the same module is even more destructive — it wipes the entire pool with a single call and equally has no caller verification: [6](#0-5) 

### Likelihood Explanation

The default configuration binds the RPC to `127.0.0.1:8114`, which limits exposure to the local machine. However:

- Many production deployments (exchanges, mining pools, dApp backends) expose the RPC to an internal network or container network, as explicitly warned against but not enforced by the code.
- On a multi-process or multi-tenant host, any local process can reach `127.0.0.1:8114` without any credential.
- The `Pool` module is enabled by default; no operator action is required to expose `remove_transaction`.
- The attacker only needs to know the `tx_hash`, which is public information visible via `get_raw_tx_pool`. [7](#0-6) 

### Recommendation

Add a submitter-identity check before allowing removal. Since CKB transactions are signed by the lock-script owner, the RPC could require the caller to provide a signature over the `tx_hash` that matches one of the input lock-script args of the transaction. Alternatively, restrict `remove_transaction` (and `clear_tx_pool`) to a separate, authenticated RPC module (similar to how `IntegrationTest` and `Debug` modules are gated), or require an operator-configured API token for all mutating pool operations.

### Proof of Concept

1. User A submits a transaction via `send_transaction` and receives `tx_hash_A`.
2. User B (any process on the same host, or any host with RPC access) calls:
   ```json
   {"jsonrpc":"2.0","method":"remove_transaction","params":["<tx_hash_A>"],"id":1}
   ```
3. The response is `{"result": true}`.
4. User A's transaction is gone from the pool. `get_transaction` returns `"status": "unknown"`. The transaction will never be confirmed unless User A resubmits it — and an attacker can repeat the removal indefinitely.

The `get_raw_tx_pool` method (also unauthenticated, also in the default `Pool` module) provides the full list of hashes needed to target every pending transaction: [8](#0-7)

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

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
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

**File:** rpc/README.md (L1-9)
```markdown
# CKB JSON-RPC Protocols

The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers.

See [list of projects](https://github.com/topics/ckb-rpc-proxy) to setup the proxy for the RPC server.
```
