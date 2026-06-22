### Title
RPC Batch Request Limit Disabled by Default Allows Unbounded Resource Exhaustion - (File: rpc/src/server.rs)

### Summary
The CKB JSON-RPC server's batch request size limit is an opt-in configuration that is **disabled by default**. Any caller with access to the RPC port can submit a single HTTP POST containing an arbitrarily large JSON array of expensive RPC calls, exhausting CPU, memory, and I/O resources of the node with no server-side enforcement.

### Finding Description
In `rpc/src/server.rs`, the `handle_jsonrpc` function processes batch requests. The batch size guard only fires when `JSONRPC_BATCH_LIMIT` has been initialized, which only happens if the operator explicitly sets `rpc_batch_limit` in the config:

```rust
// rpc/src/server.rs lines 274-282
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // processes ALL calls with no limit if JSONRPC_BATCH_LIMIT is None
``` [1](#0-0) 

`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is only initialized when `config.rpc_batch_limit` is `Some(...)`:

```rust
// rpc/src/server.rs lines 53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [2](#0-1) 

The config field is `Option<usize>` with no default value, meaning it is `None` unless explicitly set:

```rust
// util/app-config/src/configs/rpc.rs line 44
pub rpc_batch_limit: Option<usize>,
``` [3](#0-2) 

The production config template explicitly acknowledges the risk but leaves the limit commented out (disabled):

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
``` [4](#0-3) 

### Impact Explanation
An attacker with access to the RPC port can send a single HTTP POST containing thousands of expensive calls (e.g., `get_block_template`, `get_raw_tx_pool?verbose=true`, `send_transaction`, `estimate_cycles`). Each call in the batch is processed sequentially via `stream::iter(calls).then(...)`, consuming CPU, memory, and shared state locks. With a 10 MiB default `max_request_body_size`, a batch of thousands of lightweight calls fits easily within the body limit. The node's RPC runtime becomes saturated, making it unresponsive to legitimate callers (miners, wallets, relayers). The `get_raw_tx_pool(verbose=true)` call alone serializes up to 180 MB of pool state per invocation; batching even a handful of these causes severe memory pressure. [5](#0-4) 

### Likelihood Explanation
The RPC is bound to `127.0.0.1:8114` by default, but the bounty scope explicitly includes "RPC caller" and "supported local CLI/RPC user" as valid attacker roles. Many production deployments expose the RPC to a local network or behind a reverse proxy accessible to multiple users. The attack requires only a single HTTP POST with a crafted JSON array — no authentication, no special privileges, no prior knowledge of chain state. The config comment itself acknowledges the risk exists and is unmitigated by default. [4](#0-3) 

### Recommendation
- Change `rpc_batch_limit` from `Option<usize>` to a required field with a safe default (e.g., 100–200 calls per batch), enforced unconditionally.
- Alternatively, initialize `JSONRPC_BATCH_LIMIT` with a hardcoded safe default when `config.rpc_batch_limit` is `None`, rather than skipping the check entirely.
- Document the risk prominently and emit a startup warning when no batch limit is configured.

### Proof of Concept
```bash
# Build a batch of 5000 get_raw_tx_pool(verbose=true) calls
python3 -c "
import json, sys
calls = [{'id': i, 'jsonrpc': '2.0', 'method': 'get_raw_tx_pool', 'params': [True]} for i in range(5000)]
print(json.dumps(calls))
" > batch.json

# Send to a node with no rpc_batch_limit configured (default)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data @batch.json
# Node RPC becomes unresponsive; memory spikes proportional to tx-pool size × 5000
```

Each `get_raw_tx_pool(verbose=true)` call acquires the tx-pool lock and serializes all entries. With a 180 MB pool (`max_tx_pool_size = 180_000_000`), 5000 sequential serializations saturate the RPC runtime and exhaust available memory. [6](#0-5) [7](#0-6)

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
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

**File:** rpc/src/server.rs (L284-296)
```rust
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

**File:** rpc/src/module/pool.rs (L703-718)
```rust
    fn get_raw_tx_pool(&self, verbose: Option<bool>) -> Result<RawTxPool> {
        let tx_pool = self.shared.tx_pool_controller();

        let raw = if verbose.unwrap_or(false) {
            let info = tx_pool
                .get_all_entry_info()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Verbose(info.into())
        } else {
            let ids = tx_pool
                .get_all_ids()
                .map_err(|err| RPCError::custom(RPCError::CKBInternalError, err.to_string()))?;
            RawTxPool::Ids(ids.into())
        };
        Ok(raw)
    }
```

**File:** util/app-config/src/configs/tx_pool.rs (L12-13)
```rust
    /// Keep the transaction pool below <max_tx_pool_size> mb
    pub max_tx_pool_size: usize,
```
