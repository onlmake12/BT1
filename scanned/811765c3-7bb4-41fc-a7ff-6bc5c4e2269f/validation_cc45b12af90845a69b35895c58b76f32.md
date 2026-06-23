### Title
Unbounded JSON-RPC Batch Request Processing Causes Application-Level DoS — (`rpc/src/server.rs`)

### Summary

CKB's JSON-RPC server supports batch requests per the JSON-RPC 2.0 specification. The batch size limit (`rpc_batch_limit`) is **disabled by default** — the `JSONRPC_BATCH_LIMIT` static is never initialized unless the operator explicitly sets the option. Any RPC caller (local or remote if the port is exposed) can submit a single HTTP/TCP/WebSocket request containing an arbitrarily large number of RPC calls, causing unbounded CPU and memory consumption on the node. This is the direct analog of GraphQL Alias Overloading and Field Duplication: the same operation repeated N times in one request, with no server-side cap.

### Finding Description

In `rpc/src/server.rs`, the global `JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is only initialized when `config.rpc_batch_limit` is `Some(...)`:

```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
// ...
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) 

In the batch handler, the guard is:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // processes ALL calls with no limit
``` [2](#0-1) 

When `rpc_batch_limit` is absent from the config (the default), `JSONRPC_BATCH_LIMIT.get()` returns `None`, the `if let Some(...)` arm is never taken, and **all calls in the batch are processed unconditionally**. The default config file explicitly documents this gap:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [3](#0-2) 

The only existing guard is `max_request_body_size = 10485760` (10 MiB). Within 10 MiB, an attacker can pack thousands of small but non-trivial RPC calls. For example, a batch of 50,000 `get_tip_block_number` calls fits well within 10 MiB (each call is ~80 bytes of JSON), and each call acquires a snapshot read lock and serializes a response. Calls like `get_block_template` or `get_cells` are far more expensive per call.

The `rpc_batch_limit` field in `RpcConfig` is typed as `Option<usize>` with no default value, confirming the limit is opt-in: [4](#0-3) 

The test suite itself hard-codes `rpc_batch_limit: Some(1000)` to enable the limit, confirming it is not active in production unless explicitly set: [5](#0-4) 

### Impact Explanation

An attacker who can reach the RPC port (local process, or remote if the operator has exposed the port) sends a single HTTP POST with a JSON array of thousands of identical or varied RPC calls. The server processes every call sequentially, consuming CPU proportional to the batch size and allocating memory for all responses before streaming them back. This can:

- Saturate the RPC worker thread pool, blocking legitimate RPC users (miners, wallets, monitoring)
- Cause memory pressure if expensive calls (`get_block_template`, `get_cells`) are batched
- Degrade or halt block template generation, impacting mining

The `max_request_body_size` cap of 10 MiB is insufficient mitigation: a 10 MiB batch of `get_tip_block_number` calls contains ~125,000 calls, each requiring a DB snapshot read and JSON serialization.

**Impact: High** — service unavailability for all RPC consumers on the node.

### Likelihood Explanation

The RPC defaults to `127.0.0.1:8114` (localhost), so remote exploitation requires the operator to have exposed the port. However:

1. Many node operators expose the RPC for wallet/dApp connectivity.
2. A local attacker (malicious process on the same host, compromised dependency, or a script running under the same user) can reach the port without any network exposure.
3. The TCP RPC endpoint (`tcp_listen_address`) and WebSocket endpoint (`ws_listen_address`) are optional but commonly enabled for subscription use, and may be bound to `0.0.0.0`.

**Likelihood: Medium** — the default localhost binding reduces exposure, but the pattern of exposing the RPC is common in practice, and local exploitation requires no privilege.

### Recommendation

1. **Set a safe default for `rpc_batch_limit`** in `RpcConfig` (e.g., 200) rather than leaving it as `Option<usize>` with no default. The limit should be enforced unconditionally, not only when the operator opts in.
2. Alternatively, change the guard in `handle_jsonrpc` to enforce a hard-coded maximum (e.g., 2000) even when `rpc_batch_limit` is not configured, and allow the config to lower it further.
3. Document the risk prominently in the default `ckb.toml` and require operators to explicitly opt out of the limit rather than opt in.

### Proof of Concept

```bash
# Build a batch of 10,000 get_tip_block_number calls (~800 KB, well under 10 MiB)
python3 -c "
import json, sys
batch = [{'id': i, 'jsonrpc': '2.0', 'method': 'get_tip_block_number', 'params': []} for i in range(10000)]
sys.stdout.write(json.dumps(batch))
" > batch.json

# Send to a default CKB node (no rpc_batch_limit configured)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data-binary @batch.json | wc -c
```

With `rpc_batch_limit` absent from `ckb.toml`, the server processes all 10,000 calls and returns a 10,000-element JSON array. Repeating this in a tight loop from a local process will saturate the RPC runtime. Substituting `get_block_template` (which involves tx-pool traversal and block assembly) amplifies the CPU cost per call by orders of magnitude.

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

**File:** rpc/src/server.rs (L274-289)
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
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```

**File:** rpc/src/tests/setup.rs (L183-185)
```rust
        threads: None,
        rpc_batch_limit: Some(1000),
        // enable all rpc modules in unit test
```
