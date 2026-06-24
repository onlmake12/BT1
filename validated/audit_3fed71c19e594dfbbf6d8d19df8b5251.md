All code references check out against the actual source. Here is the validated output:

---

Audit Report

## Title
Unbounded JSON-RPC Batch Request Causes Local RPC Server Resource Exhaustion — (File: rpc/src/server.rs)

## Summary
`JSONRPC_BATCH_LIMIT` is only initialized when `config.rpc_batch_limit` is explicitly set in the configuration. The shipped default leaves `rpc_batch_limit` commented out, so the static `OnceLock` is never populated and the batch-size guard is never entered. A local process can send a single HTTP POST containing tens of thousands of sequential RPC calls within the 10 MiB body limit, saturating the server's processing capacity and making the RPC interface unresponsive.

## Finding Description
`JSONRPC_BATCH_LIMIT` is declared as `static JSONRPC_BATCH_LIMIT: OnceLock<usize>` at line 34 of `rpc/src/server.rs`. It is populated only inside the conditional block at lines 53–55:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

When `rpc_batch_limit` is absent from the config, `JSONRPC_BATCH_LIMIT.get()` returns `None`, so the guard at lines 274–282 is never entered:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    ...
}
```

Batch calls are dispatched **sequentially** via `.then()` at lines 284–289 — each call must complete before the next begins. The only constraint is `max_request_body_size = 10485760` (10 MiB) at line 187 of `resource/ckb.toml`. The default config at lines 205–208 of `resource/ckb.toml` explicitly leaves `rpc_batch_limit` commented out with a comment acknowledging the risk. The `rpc_batch_limit` field in `util/app-config/src/configs/rpc.rs` line 44 is typed `Option<usize>` with no `#[serde(default)]` forcing a value.

## Impact Explanation
A local process can render the node's RPC interface unresponsive for the duration of the attack. This matches the allowed bounty impact: **"Any local RPC API crash" (Note, 0–500 points)**. The RPC endpoint defaults to `127.0.0.1:8114` (localhost only), so the attacker must be a local process. The `TimeoutLayer` at 30 seconds (lines 125–128 of `server.rs`) applies per-connection but does not prevent multiple concurrent oversized batches from queuing in the Tokio runtime.

## Likelihood Explanation
The RPC endpoint is localhost-only by default. Any local process — a compromised dependency, a script, or a malicious application running on the same machine — can reach it without credentials. The attack requires only a standard HTTP POST with a JSON array body. The default configuration ships with the protection disabled, and the config comment itself acknowledges the risk, making the attack straightforward for any local attacker.

## Recommendation
Set a safe hard-coded default for `JSONRPC_BATCH_LIMIT` that applies even when `rpc_batch_limit` is absent from the configuration:

```rust
const DEFAULT_BATCH_LIMIT: usize = 2000;
let limit = config.rpc_batch_limit.unwrap_or(DEFAULT_BATCH_LIMIT);
let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| limit);
```

Additionally, consider replacing the sequential `.then()` dispatch with bounded concurrency (e.g., `buffer_unordered(N)`) so a single slow call does not block the entire batch response stream.

## Proof of Concept
```python
import json, requests

batch = [
    {"jsonrpc": "2.0", "method": "get_tip_block_number", "params": [], "id": i}
    for i in range(100_000)
]
payload = json.dumps(batch)
# ~5.5 MiB — within the 10 MiB max_request_body_size limit

resp = requests.post(
    "http://127.0.0.1:8114",
    data=payload,
    headers={"Content-Type": "application/json"},
    timeout=120,
)
print(resp.status_code)
# While this request is processing, concurrent legitimate RPC calls
# will queue or time out. Repeat with concurrent connections to multiply load.
```

---

**Code references verified:**

- `JSONRPC_BATCH_LIMIT` static declaration: [1](#0-0) 
- Conditional initialization (only when `Some`): [2](#0-1) 
- Batch guard that is never entered when config is absent: [3](#0-2) 
- Sequential `.then()` dispatch: [4](#0-3) 
- `TimeoutLayer` at 30 seconds: [5](#0-4) 
- `rpc_batch_limit: Option<usize>` with no default: [6](#0-5) 
- Default config leaves `rpc_batch_limit` commented out: [7](#0-6) 
- `max_request_body_size = 10485760`: [8](#0-7)

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

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
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
