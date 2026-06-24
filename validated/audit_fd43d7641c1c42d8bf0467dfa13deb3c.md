Audit Report

## Title
No Default Enforcement of JSON-RPC Batch Request Size Limit Enables Local Resource Exhaustion - (File: `rpc/src/server.rs`)

## Summary
`rpc_batch_limit` is declared as `Option<usize>` with no default value in the `Config` struct. The global `JSONRPC_BATCH_LIMIT` static is only populated when an operator explicitly sets the option, so the batch-size guard in `handle_jsonrpc` is never enforced in a default deployment. Any local process can submit arbitrarily large batch requests bounded only by the 10 MiB body limit, sustaining CPU pressure through repeated requests despite the 30-second `TimeoutLayer`.

## Finding Description
`rpc_batch_limit` carries no default and has no `#[serde(default)]` attribute: [1](#0-0) 

At startup, `JSONRPC_BATCH_LIMIT` is only initialized when the operator has explicitly set the option: [2](#0-1) 

The batch guard in `handle_jsonrpc` is gated on `JSONRPC_BATCH_LIMIT.get()` returning `Some`; when it returns `None` (the default), all calls in the batch proceed unconditionally: [3](#0-2) 

The shipped default configuration explicitly leaves the limit commented out: [4](#0-3) 

The `TimeoutLayer` of 30 seconds caps any single request but does not prevent an attacker from immediately resubmitting a maximally-sized batch in a loop: [5](#0-4) 

The TCP path (`start_tcp_server`) has no equivalent batch-size guard at all: [6](#0-5) 

Note: the PoC asserts 200,000 calls fit under 10 MiB, but at ~67 bytes per call that body is approximately 13 MiB and would be rejected by the `max_request_body_size = 10485760` limit. The attack remains valid with approximately 100,000–150,000 calls, which do fit within 10 MiB and still constitute a large unbounded batch. [7](#0-6) 

## Impact Explanation
The RPC endpoint binds to `127.0.0.1:8114` by default, restricting the attack surface to local callers. Any local process can submit repeated large batch requests, pegging CPU and degrading block processing, transaction relay, and peer synchronization. This maps to **Note (0–500 points): Any local RPC API crash**.

## Likelihood Explanation
No authentication is required. Any local process qualifies as an attacker against the default localhost binding. The attack requires a single HTTP POST and is trivially repeatable in a loop. The 30-second timeout limits per-request damage but does not prevent sustained flooding. The unit test setup explicitly sets `rpc_batch_limit: Some(1000)`, confirming the developers are aware the default `None` is unprotected. [8](#0-7) 

## Recommendation
1. Set a safe non-`None` default for `rpc_batch_limit` in the `Config` struct (e.g., 500–2000 calls) so the limit is enforced without operator action.
2. Enforce the limit unconditionally in `handle_jsonrpc` rather than only when `JSONRPC_BATCH_LIMIT` is populated.
3. Apply an equivalent batch limit to the TCP path (`start_tcp_server`), which currently has no guard.

## Proof of Concept
```python
import json, requests, itertools

# ~100k calls fit within the 10 MiB body limit (~67 bytes each)
calls = [{"jsonrpc":"2.0","method":"get_tip_block_number","params":[],"id":i}
         for i in range(100_000)]
body = json.dumps(calls)
assert len(body.encode()) < 10 * 1024 * 1024  # verified under 10 MiB

# Repeat to sustain resource exhaustion; each request runs for up to 30s
for _ in itertools.count():
    requests.post("http://127.0.0.1:8114", data=body,
                  headers={"Content-Type": "application/json"}, timeout=35)
```
With `rpc_batch_limit` unset (the default), the node processes all calls in each batch until the 30-second timeout fires, then immediately accepts the next request. Node CPU remains pegged and block processing degrades for the duration.

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

**File:** rpc/src/server.rs (L156-202)
```rust
    async fn start_tcp_server(
        rpc: Arc<MetaIoHandler<Option<Session>>>,
        tcp_listen_address: String,
        handler: Handle,
    ) -> Result<SocketAddr, AnyError> {
        // TCP server with line delimited json codec.
        let listener = TcpListener::bind(tcp_listen_address).await?;
        let tcp_address = listener.local_addr()?;
        handler.spawn(async move {
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
            let stream_config = StreamServerConfig::default()
                .with_channel_size(4)
                .with_pipeline_size(4)
                .with_shutdown(async move {
                    new_tokio_exit_rx().cancelled().await;
                });

            let exit_signal: CancellationToken = new_tokio_exit_rx();
            tokio::select! {
                _ = async {
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
                    } => {},
                _ = exit_signal.cancelled() => {
                    info!("TCP RPCServer stopped");
                }
            }
        });
        Ok(tcp_address)
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

**File:** rpc/src/tests/setup.rs (L184-184)
```rust
        rpc_batch_limit: Some(1000),
```
