The code path is confirmed. Here is the analysis:

**Key facts from the code:**

**1. `JSONRPC_BATCH_LIMIT` is only initialized when `rpc_batch_limit` is explicitly set:** [1](#0-0) 

If `config.rpc_batch_limit` is `None` (the default), the `OnceLock` is never initialized.

**2. The batch guard is conditional on the `OnceLock` being populated:** [2](#0-1) 

`JSONRPC_BATCH_LIMIT.get()` returns `None` → the `if let Some(...)` arm is never entered → the guard is entirely skipped → `stream::iter(calls).then(...)` processes all N calls with no upper bound.

**3. The default config explicitly leaves `rpc_batch_limit` commented out:** [3](#0-2) 

The comment even acknowledges the risk: *"By default, there is no limitation on the size of batch request size."*

**4. `max_request_body_size` is defined in config but not applied to the axum router:** [4](#0-3) 

The `start_server` function applies `CorsLayer` and `TimeoutLayer` but no `RequestBodyLimitLayer` using `config.max_request_body_size`. The field exists in the config struct but is unused in the HTTP server setup.

**5. The 30-second `TimeoutLayer` is the only real bound**, but it does not prevent resource exhaustion during that window — it only terminates the connection after the damage is done.

**6. Default RPC binding is localhost-only:** [5](#0-4) 

This is a meaningful constraint. In the strict default config, only local processes can reach the RPC. However, many production deployments expose the RPC port, and the vulnerability exists regardless of binding — any process that can reach the endpoint (local or remote) can exploit it.

---

### Title
Unbounded JSON-RPC Batch Processing When `rpc_batch_limit` Is Absent — (`rpc/src/server.rs`)

### Summary
When `rpc_batch_limit` is not set in `ckb.toml` (the default), `JSONRPC_BATCH_LIMIT` (`OnceLock<usize>`) is never initialized. The batch-size guard in `handle_jsonrpc` uses `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()`, which silently skips the check when the lock is unset. Any caller that can reach the RPC endpoint can send a single HTTP POST containing an arbitrarily large JSON-RPC batch array, causing the Tokio async runtime to process all calls sequentially with no bound.

### Finding Description
In `rpc/src/server.rs`, `JSONRPC_BATCH_LIMIT` is a `static OnceLock<usize>`: [6](#0-5) 

It is initialized only when `config.rpc_batch_limit` is `Some(...)`: [1](#0-0) 

The batch guard in `handle_jsonrpc` is: [7](#0-6) 

When the `OnceLock` is unset (default), `get()` returns `None`, the `if let Some(...)` arm is never entered, and the guard is completely bypassed. Processing then falls through to: [8](#0-7) 

All N calls are processed sequentially with no upper bound. Additionally, `max_request_body_size` from the config is never applied to the axum router (no `RequestBodyLimitLayer` is present in `start_server`), so the body size is also effectively unbounded.

### Impact Explanation
A single HTTP POST with a large batch of compute-heavy calls (e.g., `estimate_cycles` with a max-cycle script) saturates the Tokio async runtime shared by the RPC server, block relay, sync, and tx-pool processing. This stalls all other node operations for the duration of the request (up to the 30-second timeout), and the attack can be repeated continuously at low cost.

### Likelihood Explanation
The default config explicitly leaves `rpc_batch_limit` unset and documents the absence of a limit. Any operator running a node with default config and an exposed RPC port (common for infrastructure nodes, mining pools, and dApps) is vulnerable. The attack requires only a single HTTP request with no authentication.

### Recommendation
Replace the `OnceLock`-based guard with a hard default. Either:
- Set a safe default value for `rpc_batch_limit` in the `Config` struct (e.g., `2000`) so the limit is always active unless explicitly overridden, or
- Change the guard to `let limit = JSONRPC_BATCH_LIMIT.get().copied().unwrap_or(DEFAULT_BATCH_LIMIT)` with a compile-time constant fallback.

Also apply `max_request_body_size` via `axum::extract::DefaultBodyLimit::max(config.max_request_body_size)` in `start_server`.

### Proof of Concept
1. Start a CKB node with default `ckb.toml` (no `rpc_batch_limit` set).
2. Send: `POST http://127.0.0.1:8114/ HTTP/1.1` with body: a JSON array of N `estimate_cycles` calls each referencing a transaction with a max-cycle script.
3. Observe: the node's RPC becomes unresponsive to concurrent requests (e.g., `ping`) for the duration of the batch processing window.
4. Repeat the POST continuously to maintain the denial-of-service condition.

### Citations

**File:** rpc/src/server.rs (L34-34)
```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
```

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
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

**File:** rpc/src/server.rs (L274-282)
```rust
            Request::Batch(calls) => {
                if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
                    && calls.len() > *batch_size
                {
                    return make_error_response(jsonrpc_core::Error::invalid_params(format!(
                        "batch size is too large, expect it less than: {}",
                        batch_size
                    )));
                }
```

**File:** rpc/src/server.rs (L284-289)
```rust
                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });
```

**File:** resource/ckb.toml (L182-183)
```text
listen_address = "127.0.0.1:8114" # {{
# _ => listen_address = "127.0.0.1:{rpc_port}"
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
