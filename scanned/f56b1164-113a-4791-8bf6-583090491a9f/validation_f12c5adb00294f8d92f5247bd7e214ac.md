### Title
Unbounded JSON-RPC Batch Request Causes RPC Server Resource Exhaustion — (`File: rpc/src/server.rs`)

### Summary

The CKB JSON-RPC server has no enforced default limit on batch request size. The `rpc_batch_limit` guard is opt-in and disabled by default. An RPC caller can send a single HTTP POST containing an arbitrarily large JSON-RPC batch array, forcing the server to process every call sequentially with no cap, exhausting CPU and memory and rendering the RPC server unresponsive.

### Finding Description

`rpc_batch_limit` is declared as `Option<usize>` with no default value. [1](#0-0) 

At startup, `JSONRPC_BATCH_LIMIT` (a `OnceLock<usize>`) is only initialized when the operator explicitly sets `rpc_batch_limit` in `ckb.toml`. When the field is absent (the default), the `OnceLock` is never written. [2](#0-1) 

The default `ckb.toml` ships with the option commented out and explicitly documents that there is no limit by default: [3](#0-2) 

In `handle_jsonrpc`, the batch-size guard is a short-circuit that only fires when `JSONRPC_BATCH_LIMIT.get()` returns `Some`. When the `OnceLock` is unset it returns `None`, the `if let Some(...)` arm is never entered, and the entire batch is processed unconditionally: [4](#0-3) 

Each call in the batch is dispatched sequentially via `.then(...)` on a `stream::iter`. There is no concurrency cap, no per-batch timeout beyond the global 30-second `TimeoutLayer`, and no memory ceiling on the accumulated response stream. [5](#0-4) 

The `max_request_body_size` is 10 MiB by default. A minimal valid JSON-RPC call (`{"jsonrpc":"2.0","method":"ping","id":1}`) is ~40 bytes, allowing ~260,000 calls per batch within the body-size limit. Expensive calls such as `get_block` (verbose), `get_transaction`, or `get_block_economic_state` amplify the CPU and memory cost per call by orders of magnitude.

### Impact Explanation

A single HTTP POST from any RPC caller can force the server to process hundreds of thousands of sequential RPC calls. This saturates the Tokio async runtime's worker threads, exhausts heap memory building the response stream, and causes the RPC server to stop responding to all other clients for the duration of the batch — effectively a complete, repeatable RPC server takedown matching the impact of the reference report.

### Likelihood Explanation

The default `ckb.toml` ships without `rpc_batch_limit` set. Any operator who uses the generated config without explicitly adding the option is vulnerable. The RPC port is localhost-bound by default, but the scope explicitly includes "RPC caller" as a valid attacker role, and many production deployments (exchanges, dApp backends, block explorers) expose the RPC port to internal networks or proxies. No authentication, no special privilege, and no prior knowledge beyond the public JSON-RPC API is required.

### Recommendation

Set a safe non-`None` default for `rpc_batch_limit` in `Config` (e.g., 2000, matching the commented-out example) so the guard is active out of the box without operator action. The `OnceLock` initialization in `RpcServer::new` should then always be populated. [1](#0-0) [6](#0-5) 

### Proof of Concept

```python
import json, requests

# Target: any CKB node with RPC accessible (default: 127.0.0.1:8114)
ENDPOINT = "http://127.0.0.1:8114"

# Build a batch of 200,000 cheap calls — fits within the 10 MiB body limit
batch = [{"jsonrpc": "2.0", "method": "ping", "id": i} for i in range(200_000)]

# Single POST; server processes all calls sequentially with no limit
resp = requests.post(ENDPOINT, json=batch, timeout=120)
# RPC server is now saturated; concurrent legitimate requests time out
```

For higher impact, replace `"ping"` with `"get_block"` (with `verbosity=2`) to amplify per-call memory and CPU cost, reducing the batch size needed to exhaust resources.

### Citations

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```

**File:** rpc/src/server.rs (L34-55)
```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();

#[doc(hidden)]
#[derive(Debug)]
pub struct RpcServer {
    pub http_address: SocketAddr,
    pub tcp_address: Option<SocketAddr>,
    pub ws_address: Option<SocketAddr>,
}

impl RpcServer {
    /// Creates an RPC server.
    ///
    /// ## Parameters
    ///
    /// * `config` - RPC config options.
    /// * `io_handler` - RPC methods handler. See [ServiceBuilder](../service_builder/struct.ServiceBuilder.html).
    /// * `handler` - Tokio runtime handle.
    pub fn new(config: RpcConfig, io_handler: IoHandler, handler: Handle) -> Self {
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L274-296)
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

                let stream = stream::iter(calls)
                    .then(move |call| {
                        let io = Arc::clone(&io);
                        async move { io.handle_call(call, T::default()).await }
                    })
                    .filter_map(|response| async move { response });

                (
                    [(axum::http::header::CONTENT_TYPE, "application/json")],
                    StreamBodyAs::json_array(stream),
                )
                    .into_response()
            }
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
