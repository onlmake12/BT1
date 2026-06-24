Audit Report

## Title
Unbounded JSON-RPC Batch Request Processing With No Default Limit - (File: `rpc/src/server.rs`)

## Summary
The CKB JSON-RPC server initializes `JSONRPC_BATCH_LIMIT` only when `config.rpc_batch_limit` is explicitly set. Because the default configuration leaves this field commented out, `JSONRPC_BATCH_LIMIT.get()` returns `None` at runtime and the batch-size guard in `handle_jsonrpc` is entirely skipped. Any caller with access to the RPC port can submit a single HTTP POST with an arbitrarily large JSON-RPC batch array, forcing the node to process every call with no cap.

## Finding Description
`JSONRPC_BATCH_LIMIT` is a `static OnceLock<usize>` that is only populated when the operator explicitly sets `rpc_batch_limit` in the config:

```rust
// rpc/src/server.rs L53-55
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
``` [1](#0-0) 

The batch guard in `handle_jsonrpc` uses `if let Some(...)` on the result of `.get()`. When the `OnceLock` was never initialized, `.get()` returns `None`, the condition is false, and the entire check is skipped:

```rust
// rpc/src/server.rs L274-282
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    {
        return make_error_response(...);
    }
    // ALL calls processed with no limit
``` [2](#0-1) 

The default config explicitly leaves the limit commented out:

```toml
# By default, there is no limitation on the size of batch request size
# rpc_batch_limit = 2000
``` [3](#0-2) 

The config struct field is `Option<usize>` with no `#[serde(default)]` providing a fallback value: [4](#0-3) 

Two partial mitigations exist but do not eliminate the issue: a `max_request_body_size` of 10 MiB (limiting absolute batch size to roughly tens of thousands of minimal calls) and a `TimeoutLayer` of 30 seconds on the HTTP server. [5](#0-4) [6](#0-5) 

## Impact Explanation
**Note (0–500 points) — Local RPC API crash/unresponsiveness.** The default bind address is `127.0.0.1:8114`, so by default only local users can reach the endpoint. A local unprivileged process can submit a single oversized batch and make the node's RPC unresponsive for up to 30 seconds (until the timeout fires), repeatedly. This matches the allowed impact class "Any local RPC API crash." The claimed High impact (network-wide node crash) requires the operator to have re-bound the RPC to a non-loopback address, which the config explicitly warns against — that precondition is operator misconfiguration, not a default-reachable exploit path, so the High severity is not supported by the evidence.

## Likelihood Explanation
Triggerable by any unprivileged local process on the node host with no authentication. Requires no special privileges, no leaked keys, and no victim interaction. Repeatability is unlimited. For the elevated (network-exposed) scenario, the operator must have overridden the default `listen_address`, which is explicitly discouraged in the shipped config.

## Recommendation
1. Add a safe default for `rpc_batch_limit` in the `Config` struct (e.g., 200) using `#[serde(default = "default_batch_limit")]` so the limit is enforced even when the operator does not set it explicitly.
2. Change `rpc_batch_limit` from `Option<usize>` to `usize` with a default, making the limit mandatory and opt-out rather than opt-in.
3. For the TCP subscription server (`start_tcp_server`), add a connection counter or semaphore to bound the number of concurrently spawned `tokio::spawn` tasks, as it currently accepts connections without limit. [7](#0-6) 

## Proof of Concept
```python
import requests

batch = [
    {"id": i, "jsonrpc": "2.0", "method": "get_block_by_number", "params": ["0x0"]}
    for i in range(10000)
]
# Single POST to localhost — no authentication required
r = requests.post("http://127.0.0.1:8114", json=batch, timeout=None)
# Node RPC is busy processing 10,000 calls; legitimate callers receive no response
# until the 30-second TimeoutLayer fires or all calls complete.
```
With `rpc_batch_limit` unset (the default), `JSONRPC_BATCH_LIMIT.get()` returns `None`, the guard at line 275 of `rpc/src/server.rs` is skipped, and all calls are dispatched. The 10 MiB body limit caps the absolute batch size but does not prevent resource exhaustion from a maximally-sized batch of expensive method calls.

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

**File:** rpc/src/server.rs (L175-194)
```rust
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
```

**File:** rpc/src/server.rs (L274-289)
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

**File:** util/app-config/src/configs/rpc.rs (L43-44)
```rust
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
```
