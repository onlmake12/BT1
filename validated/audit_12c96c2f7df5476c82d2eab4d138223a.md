Looking at the RPC server implementation, the `remove_transaction` and `clear_tx_pool` RPC methods perform privileged pool-management actions with no caller identity check, and the RPC server is configured with `CorsLayer::permissive()`, meaning any web origin can reach these methods on a locally-running node via a cross-origin browser request.

---

### Title
Any Web Origin Can Remove Arbitrary Pending Transactions via Permissive CORS on Privileged Pool RPC Methods — (`rpc/src/server.rs`)

### Summary

The CKB RPC server applies `CorsLayer::permissive()` globally, allowing any web origin to issue cross-origin requests to the local RPC endpoint. Privileged pool-management methods — `remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue` — carry no caller-identity check. A malicious web page visited by a node operator can silently remove any pending transaction from the pool or wipe the pool entirely, without the operator's knowledge or consent.

### Finding Description

`rpc/src/server.rs` builds the Axum router with a globally permissive CORS policy:

```rust
let app = Router::new()
    ...
    .layer(CorsLayer::permissive())   // ← allows any origin
    ...
``` [1](#0-0) 

`CorsLayer::permissive()` responds to every browser preflight with `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Methods: *`, and `Access-Control-Allow-Headers: *`. This means a browser will honour cross-origin `POST` requests to `http://127.0.0.1:8114` from any page.

The `remove_transaction` implementation performs no check on who is calling it:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
``` [2](#0-1) 

Likewise `clear_tx_pool` and `clear_tx_verify_queue` contain no caller restriction: [3](#0-2) 

The RPC config struct has no authentication field; the only intended protection is the default `listen_address = "127.0.0.1:8114"`: [4](#0-3) 

But `CorsLayer::permissive()` defeats that protection for browser-based attackers: modern browsers allow cross-origin requests to `127.0.0.1` and will expose the response to JavaScript once the server returns permissive CORS headers — which this server always does.

### Impact Explanation

An attacker who controls any web page (including a compromised ad or third-party script on a legitimate site) visited by a CKB node operator can:

1. Call `remove_transaction` with a known tx hash to silently evict any pending transaction from the pool — preventing confirmation at a strategically chosen moment.
2. Call `clear_tx_pool` to wipe all pending transactions simultaneously.
3. Call `clear_tx_verify_queue` to discard all transactions awaiting verification.
4. Call `set_ban` / `clear_banned_addresses` to manipulate the peer ban list.

This is a direct analogue to the Ajna finding: just as anyone could call `Pool.transferLPs` to move LP tokens at an inappropriate time (impacting the owner's position management), here anyone can call `remove_transaction` to evict a transaction at an inappropriate time, disrupting the submitter's transaction strategy (e.g., time-sensitive DeFi operations, DAO votes, or Nervos DAO withdrawals).

### Likelihood Explanation

- The default RPC binding is `127.0.0.1:8114`, so the attack surface is every browser tab open on the operator's machine.
- No special configuration change is required by the victim.
- The attacker only needs to serve a page with a `fetch("http://127.0.0.1:8114", { method: "POST", ... })` call.
- Transaction hashes are public on-chain or observable from the mempool, so the attacker can target specific transactions.

### Recommendation

1. **Remove `CorsLayer::permissive()`** and replace it with a restrictive policy that allows only explicitly trusted origins (e.g., same-origin or a configurable allowlist). The default should be to allow no cross-origin access.
2. **Add an authentication token** (e.g., a bearer token or HTTP Basic Auth) to all privileged RPC methods (`remove_transaction`, `clear_tx_pool`, `set_ban`, etc.), checked server-side before dispatch.
3. Alternatively, expose privileged methods only on a separate, non-browser-reachable transport (e.g., a Unix domain socket).

### Proof of Concept

A malicious page served from any origin (e.g., `https://evil.example`) can execute:

```javascript
// Remove a specific pending transaction from the victim's node
fetch("http://127.0.0.1:8114", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    id: 1, jsonrpc: "2.0", method: "remove_transaction",
    params: ["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"]
  })
});
```

Because `CorsLayer::permissive()` answers the browser's preflight with `Access-Control-Allow-Origin: *`, the browser sends the actual POST and the node removes the transaction — with no authentication, no caller check, and no indication to the operator. [1](#0-0) [2](#0-1)

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

**File:** util/app-config/src/configs/rpc.rs (L24-61)
```rust
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Eq)]
#[serde(deny_unknown_fields)]
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```
