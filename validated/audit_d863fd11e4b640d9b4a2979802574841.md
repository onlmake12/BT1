Audit Report

## Title
No Rate Limiting Combined with Disabled Batch Limit Enables CPU/Thread Exhaustion via Concurrent `test_tx_pool_accept` Batch Requests — (File: `rpc/src/server.rs`, `tx-pool/src/process.rs`)

## Summary
The CKB JSON-RPC server has no per-connection or per-IP rate limiting, and the batch request size cap (`rpc_batch_limit`) is disabled by default. The `test_tx_pool_accept` RPC method invokes full CKB-VM script verification via `block_in_place`, blocking a Tokio worker thread for the duration of each call. An attacker with access to the RPC port can send many concurrent HTTP connections each carrying an unbounded batch of `test_tx_pool_accept` calls, causing Tokio to spawn replacement worker threads without bound, exhausting OS thread limits and rendering the node unresponsive.

## Finding Description

**1. No rate limiting on the RPC server.**
`start_server` in `rpc/src/server.rs` builds the Axum router with only `CorsLayer::permissive()` and a 30-second `TimeoutLayer`. There is no connection limit, no per-IP throttle, and no concurrency-limiting middleware. [1](#0-0) 

**2. Batch size limit is off by default.**
`JSONRPC_BATCH_LIMIT` is only populated when `config.rpc_batch_limit` is explicitly set. The guard at lines 275–282 is never entered when the option is absent, meaning any batch size is accepted. [2](#0-1) [3](#0-2) 

**3. Batch calls are processed sequentially per connection.**
The batch handler uses `stream::iter(calls).then(...)`, which is sequential within a single connection. Multiple concurrent connections each run their own sequential batch, multiplying the thread-blocking effect. [4](#0-3) 

**4. `_test_accept_tx` runs full CKB-VM verification via `block_in_place`.**
`_test_accept_tx` calls `verify_rtx` with `self.consensus.max_block_cycles()` (~3.5B cycles) and passes `None` for `command_rx`. In `verify_rtx`, the `None` command_rx branch falls through to `block_in_place`, which blocks the current Tokio worker thread and causes Tokio to spawn a new replacement worker thread. [5](#0-4) [6](#0-5) 

**5. `PoolIsFull` rejection does not apply.**
`test_tx_pool_accept` is a dry-run path that does not insert into the pool, so pool-full guards that limit `send_transaction` flooding are irrelevant. [7](#0-6) 

**Root cause:** The combination of (a) no batch size limit by default, (b) no connection/concurrency limit, and (c) `block_in_place` with `max_block_cycles` in the dry-run path means each batch call in each concurrent connection blocks a worker thread and forces Tokio to spawn a new one. With K concurrent connections each carrying N calls, up to K×N threads can be spawned simultaneously, exhausting OS thread limits.

## Impact Explanation
An attacker with access to the RPC port can exhaust OS thread resources, stalling the Tokio runtime. This causes the tx-pool service queue to back up, P2P sync and block submission to stall, and the node to become unresponsive. The default binding is `127.0.0.1:8114` (localhost only), which limits remote exploitation. For a locally accessible or exposed RPC port, this matches **Note (0–500 points): Any local RPC API crash**, with potential escalation to **High (10001–15000 points): Vulnerabilities which could easily crash a CKB node** when the RPC port is exposed to a network (a common operator configuration).

## Likelihood Explanation
The default `127.0.0.1` binding requires local access or an exposed RPC port. No authentication, tokens, or special privileges are required beyond network access to the port. The `rpc_batch_limit` guard is off by default. The attack requires only standard HTTP POST requests with a crafted JSON body, is repeatable, and requires no victim interaction. Many operators expose the RPC port to internal networks or behind reverse proxies.

## Recommendation

1. **Enable `rpc_batch_limit` by default** in `resource/ckb.toml` (e.g., `rpc_batch_limit = 200`) rather than leaving it commented out.
2. **Add a concurrency-limiting middleware** (e.g., `tower::limit::ConcurrencyLimitLayer`) to the Axum router in `rpc/src/server.rs` `start_server` to cap simultaneous in-flight requests.
3. **Add a connection limit** to the TCP accept loop in `start_tcp_server` (`rpc/src/server.rs` lines 176–194), which currently spawns an unbounded number of tasks.
4. **Cap cycles for `test_tx_pool_accept`** at `max_tx_verify_cycles` (from tx-pool config, currently 70M) rather than `max_block_cycles` (~3.5B) in `_test_accept_tx`, consistent with how regular submission is bounded.

## Proof of Concept

```bash
# Generate a batch of 500 test_tx_pool_accept calls
python3 -c "
import json
call = {
  'jsonrpc': '2.0', 'method': 'test_tx_pool_accept', 'id': 1,
  'params': [{'version':'0x0','cell_deps':[],'header_deps':[],
              'inputs':[{'previous_output':{'tx_hash':'0x'+'a'*64,'index':'0x0'},'since':'0x0'}],
              'outputs':[],'outputs_data':[],'witnesses':[]}, None]
}
print(json.dumps([call]*500))
" > batch.json

# Launch 50 concurrent connections
for i in $(seq 1 50); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d @batch.json &
done
wait
# Each connection enters the sequential batch handler;
# each call hits _test_accept_tx -> verify_rtx -> block_in_place(ContextualTransactionVerifier)
# with max_block_cycles, causing Tokio to spawn replacement worker threads without bound.
```

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L119-129)
```rust
        let app = Router::new()
            .route("/", method_router.clone())
            .route("/{*path}", method_router)
            .route("/ping", get(ping_handler))
            .layer(Extension(Arc::clone(rpc)))
            .layer(CorsLayer::permissive())
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
            .layer(Extension(stream_config));
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

**File:** tx-pool/src/process.rs (L779-800)
```rust
    pub(crate) async fn _test_accept_tx(&self, tx: TransactionView) -> Result<Completed, Reject> {
        let (pre_check_ret, snapshot) = self.pre_check(&tx).await;

        let (_tip_hash, rtx, status, _fee, _tx_size) = pre_check_ret?;

        // skip check the delay window

        let verify_cache = self.fetch_tx_verify_cache(&tx).await;
        let max_cycles = self.consensus.max_block_cycles();
        let tip_header = snapshot.tip_header();
        let tx_env = Arc::new(status.with_env(tip_header));

        verify_rtx(
            Arc::clone(&snapshot),
            Arc::clone(&rtx),
            tx_env,
            &verify_cache,
            max_cycles,
            None,
        )
        .await
    }
```

**File:** tx-pool/src/util.rs (L116-131)
```rust
    } else {
        block_in_place(|| {
            ContextualTransactionVerifier::new(Arc::clone(&rtx), consensus, data_loader, tx_env)
                .verify(max_tx_verify_cycles, false)
                .and_then(|result| {
                    DaoScriptSizeVerifier::new(
                        rtx,
                        snapshot.cloned_consensus(),
                        snapshot.as_data_loader(),
                    )
                    .verify()?;
                    Ok(result)
                })
                .map_err(Reject::Verification)
        })
    }
```

**File:** rpc/src/module/pool.rs (L637-660)
```rust
    fn test_tx_pool_accept(
        &self,
        tx: Transaction,
        outputs_validator: Option<OutputsValidator>,
    ) -> Result<EntryCompleted> {
        let tx: packed::Transaction = tx.into();
        let tx: core::TransactionView = tx.into_view();

        self.check_output_validator(outputs_validator, &tx)?;

        let tx_pool = self.shared.tx_pool_controller();

        let test_accept_tx_reslt = tx_pool.test_accept_tx(tx).map_err(|e| {
            error!("Send test_tx_pool_accept_tx request error {}", e);
            RPCError::ckb_internal_error(e)
        })?;

        test_accept_tx_reslt
            .map(|test_accept_result| test_accept_result.into())
            .map_err(|reject| {
                error!("Send test_tx_pool_accept_tx request error {}", reject);
                RPCError::from_submit_transaction_reject(&reject)
            })
    }
```
