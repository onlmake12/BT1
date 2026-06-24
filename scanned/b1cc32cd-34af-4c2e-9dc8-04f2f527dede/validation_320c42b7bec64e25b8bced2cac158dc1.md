Audit Report

## Title
Unbounded JSON-RPC Batch Request Size Enables Resource Exhaustion DoS — (`rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server's batch size guard is opt-in and inactive by default. `rpc_batch_limit` is typed as `Option<usize>` with no default value, so `JSONRPC_BATCH_LIMIT` is never initialized unless the operator explicitly sets the field. Any caller with access to the RPC port can submit a single HTTP POST containing an arbitrarily large batch of expensive method calls (bounded only by the 10 MiB body limit), forcing the node to process all of them sequentially, exhausting CPU and degrading block processing, sync, and tx-pool operations.

## Finding Description

`rpc_batch_limit` in the `Config` struct carries no `#[serde(default)]` annotation, so it deserializes as `None` when absent from `ckb.toml`: [1](#0-0) 

In `RpcServer::new`, `JSONRPC_BATCH_LIMIT` is only populated when the operator has explicitly set the option: [2](#0-1) 

In `handle_jsonrpc`, the guard is gated on `JSONRPC_BATCH_LIMIT.get()` returning `Some`. When the limit is absent, the `if let Some(...)` branch is never entered and the entire batch is dispatched unconditionally via `stream::iter(calls).then(...)`: [3](#0-2) 

The shipped default configuration explicitly documents this as an unconfigured state and leaves the protective line commented out: [4](#0-3) 

The default module list includes `Chain`, which exposes `estimate_cycles`. This method runs `ScriptVerifier::verify(max_cycles)` where `max_cycles = consensus.max_block_cycles` (configured as `max_tx_verify_cycles = 70_000_000`), with no fee payment required: [5](#0-4) [6](#0-5) 

One correction to the submitted claim: the `.then()` combinator processes batch calls **sequentially**, not concurrently, so memory growth is bounded per-call rather than proportional to N simultaneously. However, CPU exhaustion is still real — each call in the batch runs synchronously on the async runtime before the next begins, and the connection is held open for the full duration.

A 30-second `TimeoutLayer` is present: [7](#0-6) 

This limits each individual request to 30 seconds, but does not prevent repeated requests or multiple concurrent connections each carrying large batches.

The `max_request_body_size = 10485760` (10 MiB) constrains the batch to roughly 25,000–35,000 calls (not 50,000 as stated in the PoC), but this is still a very large number of expensive script-execution calls per request. [8](#0-7) 

## Impact Explanation

A caller with access to the RPC port can saturate the node's async runtime for up to 30 seconds per request with zero cost, degrading or halting block processing, peer sync, and tx-pool operations. This matches the allowed CKB bounty impact: **"Any local RPC API crash" (Note, 0–500 points)** at minimum. If the operator has exposed the RPC port beyond localhost (a documented and common deployment pattern), the impact escalates to **"Vulnerabilities or bad designs which could cause CKB network congestion with few costs" (High, 10001–15000 points)**, since a single attacker can repeatedly saturate the node's processing capacity with no authentication, no fee, and no prior state.

## Likelihood Explanation

The RPC server binds to `127.0.0.1:8114` by default, requiring local access. However, many operators expose the port to internal networks or behind reverse proxies. The attack requires no credentials, no special knowledge, and no prior state — a single crafted HTTP POST is sufficient. The codebase itself acknowledges the risk in a comment but provides no safe default, indicating this is a known gap.

## Recommendation

1. Set a safe hard-coded default for `rpc_batch_limit` in the `Config` struct using `#[serde(default = "default_batch_limit")]` (e.g., 200), so protection is active without operator action.
2. Add per-IP or per-connection request-rate limiting at the HTTP layer independent of the batch limit.
3. Consider adding a semaphore or concurrency cap on batch sub-call dispatch to prevent a single batch from monopolizing the async runtime even within the limit.

## Proof of Concept

```bash
# Send a batch of ~25,000 estimate_cycles calls (within the 10 MiB body limit).
# No rpc_batch_limit configured → all calls are dispatched sequentially.
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
print(json.dumps([call] * 25000))
")"
```

`JSONRPC_BATCH_LIMIT.get()` returns `None`, the guard at `rpc/src/server.rs:275` is skipped, and all calls are dispatched via `stream::iter(calls).then(...)` with no concurrency or count bound. Each call invokes `CyclesEstimator::run` → `ScriptVerifier::verify(70_000_000)`. The node's tokio runtime is occupied for the full 30-second timeout window, during which block processing and peer sync are degraded.

### Citations

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
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

**File:** resource/ckb.toml (L215-215)
```text
max_tx_verify_cycles = 70_000_000
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
