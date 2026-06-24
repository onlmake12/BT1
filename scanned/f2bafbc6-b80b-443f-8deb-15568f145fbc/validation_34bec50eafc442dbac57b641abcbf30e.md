Audit Report

## Title
No Rate Limiting and Disabled-by-Default Batch Limit Enable DoS via Unbounded `test_tx_pool_accept` Batch Requests — (File: `rpc/src/server.rs`, `tx-pool/src/process.rs`)

## Summary
The CKB JSON-RPC server applies no per-connection or per-IP rate limiting, and the batch request size guard (`JSONRPC_BATCH_LIMIT`) is disabled by default because `rpc_batch_limit` is commented out in the shipped configuration. An attacker with access to the RPC port can send a single HTTP POST containing an arbitrarily large JSON-RPC batch of `test_tx_pool_accept` calls. Each call triggers full CKB-VM script verification via `block_in_place`, and because the tx-pool service is a single-actor channel consumer, flooding it with these requests starves all other tx-pool operations and can render the node unresponsive.

## Finding Description

**1. No rate limiting on the Axum router**

`start_server` in `rpc/src/server.rs` builds the router with only `CorsLayer::permissive()` and a 30-second `TimeoutLayer`. There is no `ConcurrencyLimitLayer`, no per-IP throttle, and no connection cap. [1](#0-0) 

**2. Batch size limit is off by default**

`JSONRPC_BATCH_LIMIT` is a `OnceLock<usize>` populated only when `config.rpc_batch_limit` is `Some(...)`. [2](#0-1) 

The shipped `resource/ckb.toml` leaves the option commented out, so `JSONRPC_BATCH_LIMIT.get()` always returns `None` and the guard at lines 275–282 is never entered. [3](#0-2) [4](#0-3) 

**3. Batch calls are processed sequentially through the tx-pool actor**

The batch handler uses `.then()`, which awaits each call before starting the next. Each `test_tx_pool_accept` call sends a message to the tx-pool service channel and blocks until the service responds. [5](#0-4) 

**4. `_test_accept_tx` runs full CKB-VM verification with `max_block_cycles`**

`_test_accept_tx` calls `verify_rtx` with `command_rx = None` and `max_block_cycles` as the cycle cap. [6](#0-5) 

With `command_rx = None`, `verify_rtx` takes the `block_in_place` branch, running `ContextualTransactionVerifier::verify` synchronously on a Tokio blocking thread. [7](#0-6) 

Because the tx-pool service is a single-actor (one message processed at a time), a long-running batch monopolises the service queue. Legitimate `send_transaction`, block-assembly, and reorg-handling messages queue behind it. Multiple concurrent batch requests from different connections compound this effect, and the 30-second HTTP timeout only bounds each individual connection, not the aggregate load.

## Impact Explanation

Flooding the tx-pool service with expensive `test_tx_pool_accept` calls causes the service queue to back up, blocking block assembly, transaction submission, and reorg processing. This can render the node unable to participate in block production or P2P sync, constituting a **node-level Denial-of-Service**. This matches the allowed impact: **High (10001–15000 points) — Vulnerabilities which could easily crash a CKB node.**

## Likelihood Explanation

The RPC binds to `127.0.0.1:8114` by default, requiring local access or an exposed port. However, many operators expose the RPC to internal networks or behind reverse proxies without authentication. The `rpc_batch_limit` guard is explicitly off by default, and the config comment acknowledges the risk. The attack requires only a single crafted HTTP POST — no credentials, no keys, no special protocol knowledge.

## Recommendation

1. **Enable `rpc_batch_limit` by default** in `resource/ckb.toml` (e.g., `rpc_batch_limit = 200`) rather than leaving it commented out. [3](#0-2) 
2. **Add a concurrency or rate-limiting middleware** (e.g., `tower::limit::ConcurrencyLimitLayer`) to the Axum router in `start_server`. [1](#0-0) 
3. **Add a connection limit** to the TCP accept loop in `start_tcp_server`, which currently spawns an unbounded number of tasks per accepted connection. [8](#0-7) 
4. Consider a per-method concurrency cap for `test_tx_pool_accept` given its synchronous CKB-VM cost. [6](#0-5) 

## Proof of Concept

```bash
# Build a batch of 2000 test_tx_pool_accept calls (rpc_batch_limit is None by default)
python3 -c "
import json
call = {
  'jsonrpc': '2.0', 'method': 'test_tx_pool_accept', 'id': 1,
  'params': [{'version':'0x0','cell_deps':[],'header_deps':[],
              'inputs':[{'previous_output':{'tx_hash':'0x'+'a'*64,'index':'0x0'},'since':'0x0'}],
              'outputs':[],'outputs_data':[],'witnesses':[]}, 'passthrough']
}
print(json.dumps([call]*2000))
" > batch.json

# Send multiple concurrent batches to saturate the tx-pool service queue
for i in $(seq 1 10); do
  curl -s -X POST http://127.0.0.1:8114 \
    -H 'Content-Type: application/json' \
    -d @batch.json &
done
wait

# Observe: legitimate RPC calls (e.g., get_tip_block_number) time out or queue indefinitely
curl -s -X POST http://127.0.0.1:8114 \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":1}'
```

Each batch call enters `_test_accept_tx` → `verify_rtx` → `block_in_place(ContextualTransactionVerifier::verify)`, monopolising the tx-pool service actor and blocking all other node operations.

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

**File:** rpc/src/server.rs (L176-194)
```rust
                        while let Ok((stream, _)) = listener.accept().await {
                            let rpc = Arc::clone(&rpc);
                            let stream_config = stream_config.clone();
                            let codec = codec.clone();
                            tokio::spawn(async move {
                                let (r, w) = stream.into_split();
                                let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
                                let w = FramedWrite::new(w, codec).with(|msg| async move {
                                    Ok::<_, LinesCodecError>(match msg {
                                        StreamMsg::Str(msg) => msg,
                                        _ => "".into(),
                                    })
                                });
                                tokio::pin!(w);
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
                                }
                            });
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
