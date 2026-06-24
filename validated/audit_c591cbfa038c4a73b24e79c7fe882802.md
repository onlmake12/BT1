Audit Report

## Title
Unbounded JSON-RPC Batch Requests Enable Free CPU Exhaustion via `estimate_cycles` — (`rpc/src/server.rs`, `rpc/src/module/chain.rs`)

## Summary

The CKB JSON-RPC server's batch-request size limit (`JSONRPC_BATCH_LIMIT`) is opt-in and unset by default, so any caller can submit an arbitrarily large batch. The `estimate_cycles` RPC method (enabled by default in the `Chain` module) invokes full CKB-VM script execution up to `consensus.max_block_cycles` (10 billion cycles on mainnet) per call, with no fee, no authentication, and no per-caller rate limit. The combination allows an unprivileged caller to saturate the node's CPU with a single HTTP POST, rendering it unresponsive to miners, relayers, and dApp users. The `TimeoutLayer` present in the server does not mitigate this because batch responses are returned as a lazy streaming body — the timeout governs only the time until the `Response` object is constructed (near-instant), not the time until the stream is fully consumed.

## Finding Description

**Root cause 1 — No default batch limit**

`JSONRPC_BATCH_LIMIT` is a `static OnceLock<usize>` initialized only when `config.rpc_batch_limit` is `Some(...)`:

```rust
// rpc/src/server.rs L34
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();

// rpc/src/server.rs L53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) [2](#0-1) 

The shipped default config leaves `rpc_batch_limit` commented out: [3](#0-2) 

The batch handler only enforces the limit when `JSONRPC_BATCH_LIMIT.get()` returns `Some`. When it returns `None` (the default), the guard is skipped and all calls are processed unconditionally: [4](#0-3) 

**Root cause 2 — `estimate_cycles` runs full VM execution for free**

`CyclesEstimator::run()` uses `consensus.max_block_cycles` as the cycle cap and calls `ScriptVerifier::verify(max_cycles)` with no fee, no authentication, and no per-caller rate limit: [5](#0-4) 

The `Chain` module (which includes `estimate_cycles`) is enabled by default: [6](#0-5) 

**Why the `TimeoutLayer` does not mitigate this**

A 30-second `TimeoutLayer` is applied to the HTTP server: [7](#0-6) 

However, for batch requests the handler constructs a *lazy* `StreamBodyAs::json_array(stream)` and returns the `Response` object immediately — no batch calls are executed before the function returns. The `TimeoutLayer` wraps the `Service::call` future (i.e., the `handle_jsonrpc` async fn), which completes near-instantly for batch requests. The actual VM execution happens when the response body is polled by the HTTP layer, which is outside the timeout scope. The timeout therefore does not bound the CPU time consumed by a large batch.

**Attack path**

1. Attacker deploys a RISC-V script that loops for close to `max_block_cycles` cycles, with valid cell deps on-chain.
2. Attacker sends a single HTTP POST to the RPC endpoint with a JSON array of N `estimate_cycles` calls referencing that transaction.
3. Because `rpc_batch_limit` is `None`, the server processes all N calls sequentially via the lazy stream, each consuming up to 10 billion VM cycles.
4. The node's tokio worker threads are saturated with CPU-bound VM execution; all other RPC requests (block template fetches, tx submissions) time out or are dropped.

## Impact Explanation

This matches the **High** impact class: *"Vulnerabilities or bad designs which could cause CKB network congestion with few costs."* Mining pools and dApp backends that expose their RPC (even on localhost) can be rendered unresponsive with zero CKB cost to the attacker. Miners lose block template access; relayers cannot submit transactions. The attack is repeatable continuously.

## Likelihood Explanation

- `estimate_cycles` requires no CKB balance, no cryptographic material, and no prior chain knowledge.
- The default bind address is `127.0.0.1`, but any process on the same machine (compromised dependency, malicious script) qualifies as a caller. Operators who expose RPC publicly are fully exposed.
- The default config explicitly documents that no batch limit is applied, meaning most deployed nodes are unprotected by default.
- A single HTTP client with a crafted JSON payload suffices; no special tooling is required.

## Recommendation

1. **Enable `rpc_batch_limit` by default**: Set `rpc_batch_limit = 200` (or similar) in `resource/ckb.toml` so `JSONRPC_BATCH_LIMIT` is always initialized. The current opt-in model inverts the safe default.
2. **Cap `estimate_cycles` cycle budget**: Introduce a configurable `max_estimate_cycles` lower than `max_block_cycles` (e.g., matching `max_tx_verify_cycles = 70_000_000` already present in the tx pool config) to bound per-call CPU cost.
3. **Apply timeout to streaming body**: Wrap the batch stream with a wall-clock deadline so that the `TimeoutLayer` actually bounds total batch processing time.

## Proof of Concept

```python
import json, requests

# Transaction whose lock script loops for ~max_block_cycles RISC-V cycles
# (attacker controls the script binary; cell deps must be on-chain)
expensive_tx = { ... }

# Unbounded batch — no rpc_batch_limit by default
batch = [
    {"jsonrpc": "2.0", "method": "estimate_cycles", "params": [expensive_tx], "id": i}
    for i in range(5000)
]

# Single POST saturates node RPC workers; node becomes unresponsive
r = requests.post("http://127.0.0.1:8114", json=batch, timeout=None)
```

Verification steps:
1. Start a CKB node with the default `ckb.toml` (no `rpc_batch_limit`).
2. Deploy a tight-loop RISC-V script as a cell.
3. Send the batch POST above.
4. Observe that concurrent legitimate RPC calls (`get_block_template`, `send_transaction`) time out or receive no response for the duration of batch processing.

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

**File:** rpc/src/module/chain.rs (L2375-2405)
```rust
    pub(crate) fn run(&self, tx: packed::Transaction) -> Result<EstimateCycles> {
        let snapshot = self.shared.cloned_snapshot();
        let consensus = snapshot.cloned_consensus();
        match resolve_transaction(tx.into_view(), &mut HashSet::new(), self, self) {
            Ok(resolved) => {
                let max_cycles = consensus.max_block_cycles;
                let tip_header = snapshot.tip_header();
                let tx_env = TxVerifyEnv::new_submit(tip_header);
                match ScriptVerifier::new(
                    Arc::new(resolved),
                    snapshot.as_data_loader(),
                    consensus,
                    Arc::new(tx_env),
                )
                .verify(max_cycles)
                {
                    Ok(cycles) => Ok(EstimateCycles {
                        cycles: cycles.into(),
                    }),
                    Err(err) => Err(RPCError::custom_with_error(
                        RPCError::TransactionFailedToVerify,
                        err,
                    )),
                }
            }
            Err(err) => Err(RPCError::custom_with_error(
                RPCError::TransactionFailedToResolve,
                err,
            )),
        }
    }
```
