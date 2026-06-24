Audit Report

## Title
RPC Batch Request Limit Disabled by Default Allows Unbounded Resource Exhaustion - (File: rpc/src/server.rs)

## Summary
The CKB JSON-RPC server's batch request size limit is opt-in and disabled by default. `JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` that is never initialized unless the operator explicitly sets `rpc_batch_limit` in the config, meaning the batch size guard is silently skipped for all default deployments. An RPC caller can submit a single HTTP POST containing an arbitrarily large JSON array of calls, consuming CPU, memory, and shared lock resources with no server-side enforcement of batch size.

## Finding Description
In `rpc/src/server.rs`, `JSONRPC_BATCH_LIMIT` is declared as a `static OnceLock<usize>` and is only initialized inside a conditional block:

```rust
// rpc/src/server.rs lines 53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) 

The batch guard in `handle_jsonrpc` only fires when `JSONRPC_BATCH_LIMIT.get()` returns `Some`. When the `OnceLock` was never initialized (the default), `get()` returns `None` and the guard is skipped entirely, proceeding to process all calls:

```rust
// rpc/src/server.rs lines 274-289
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    { ... }
    // No limit applied when JSONRPC_BATCH_LIMIT is None
    let stream = stream::iter(calls).then(...).filter_map(...);
    StreamBodyAs::json_array(stream).into_response()
}
``` [2](#0-1) 

The config field has no default value: [3](#0-2) 

The production config template explicitly acknowledges the risk but leaves the limit commented out: [4](#0-3) 

**Partial mitigations that limit but do not eliminate the issue:**
- A 30-second `TimeoutLayer` is applied to the HTTP server, which bounds the wall-clock duration of any single batch request. [5](#0-4) 
- `max_request_body_size = 10485760` (10 MiB) bounds the total number of calls per batch (roughly ~100,000 minimal calls fit within 10 MiB). [6](#0-5) 

These mitigations reduce but do not eliminate the attack surface: within the 30-second window, a batch of thousands of `get_raw_tx_pool(verbose=true)` calls each acquires the tx-pool lock and serializes pool state, causing RPC unresponsiveness for the duration of the attack window. [7](#0-6) 

## Impact Explanation
The concrete impact is local RPC API unresponsiveness. The node's P2P, consensus, and storage subsystems are unaffected; only the RPC service becomes slow or unresponsive during the attack window. This maps to **Note (0–500 points): Any local RPC API crash**. The claim's assertion of "High: easily crash a CKB node" is not supported — the node itself does not crash, and the 30-second timeout bounds each attack window. The severity is Note, not High.

## Likelihood Explanation
The RPC is bound to `127.0.0.1:8114` by default, restricting the attacker to a local or proxied caller. The bounty scope includes "RPC caller" as a valid attacker role. No authentication, special privileges, or chain state knowledge is required — only a single crafted HTTP POST. The attack is repeatable and requires no setup beyond RPC access. [8](#0-7) 

## Recommendation
- Change `rpc_batch_limit` from `Option<usize>` to a field with a safe hardcoded default (e.g., 200), enforced unconditionally when `config.rpc_batch_limit` is `None`.
- Alternatively, initialize `JSONRPC_BATCH_LIMIT` with a safe default at startup when no value is configured, rather than skipping the check entirely.
- Emit a startup warning when no batch limit is configured. [1](#0-0) 

## Proof of Concept
```bash
# Build a batch of 5000 get_raw_tx_pool(verbose=true) calls (~350 KB, within 10 MiB limit)
python3 -c "
import json
calls = [{'id': i, 'jsonrpc': '2.0', 'method': 'get_raw_tx_pool', 'params': [True]} for i in range(5000)]
print(json.dumps(calls))
" > batch.json

# Send to a node with no rpc_batch_limit configured (default)
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  --data @batch.json
# RPC becomes unresponsive for up to 30 seconds; concurrent legitimate callers are delayed
```

Each call acquires the tx-pool lock and serializes pool state. With `max_tx_pool_size = 180_000_000` bytes, repeated serialization within the 30-second timeout window causes measurable RPC latency degradation for concurrent callers. [9](#0-8)

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

**File:** util/app-config/src/configs/rpc.rs (L44-44)
```rust
    pub rpc_batch_limit: Option<usize>,
```

**File:** resource/ckb.toml (L182-182)
```text
listen_address = "127.0.0.1:8114" # {{
```

**File:** resource/ckb.toml (L187-187)
```text
max_request_body_size = 10485760
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```

**File:** resource/ckb.toml (L211-211)
```text
max_tx_pool_size = 180_000_000 # 180mb
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
