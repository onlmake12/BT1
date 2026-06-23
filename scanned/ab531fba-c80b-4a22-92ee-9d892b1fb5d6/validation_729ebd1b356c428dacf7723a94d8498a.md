### Title
TCP RPC Server Bypasses `JSONRPC_BATCH_LIMIT` Entirely, Allowing Unbounded Batch Execution — (`rpc/src/server.rs`)

### Summary

The `JSONRPC_BATCH_LIMIT` guard is implemented exclusively inside `handle_jsonrpc`, the axum HTTP handler. The TCP server path uses `serve_stream_sink` directly and never routes through `handle_jsonrpc`. This means the batch limit is silently bypassed for all TCP connections — even when `rpc_batch_limit` is explicitly configured. Combined with the 2 MB `LinesCodec` frame limit, a single TCP connection can submit an arbitrarily large batch of `get_fee_rate_statistics` calls, each performing up to ~202 synchronous DB reads.

### Finding Description

**Two code paths, one guard missing:**

The HTTP handler enforces the batch limit:

```
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    { return error; }
    ...
}
``` [1](#0-0) 

The TCP server never calls `handle_jsonrpc`. It calls `serve_stream_sink` directly with the raw `MetaIoHandler`:

```rust
serve_stream_sink(&rpc, w, r, stream_config).await
``` [2](#0-1) 

`serve_stream_sink` is from the external `jsonrpc_utils` crate and passes each decoded line directly to `MetaIoHandler`, which natively handles JSON-RPC batch arrays — with no batch size check interposed.

The `LinesCodec` frame limit is 2 MB: [3](#0-2) 

A minimal `get_fee_rate_statistics` call is ~70 bytes, so a single 2 MB line can carry ~29,000 calls. Even a conservative 200-call batch is well within the limit.

**Per-call DB cost in `FeeRateCollector::statistics`:**

Each call iterates up to `MAX_TARGET = 101` blocks, calling `get_block_hash` + `get_block_ext` per block — up to 202 DB reads: [4](#0-3) [5](#0-4) 

200 calls × 202 reads = **40,400 synchronous RocksDB reads** from one TCP frame.

**`pipeline_size=4` does not help:** it limits concurrent *messages* (lines), not calls within a single batch message. One 2 MB line is one message; all its batch calls execute together inside that single message dispatch. [6](#0-5) 

**`rpc_batch_limit` is `None` by default** (commented out in the default config): [7](#0-6) 

Even if an operator sets it, the TCP path ignores it entirely.

### Impact Explanation

A single TCP connection sending one newline-terminated 2 MB JSON batch saturates RocksDB I/O and the Tokio thread pool with tens of thousands of DB reads. This degrades block processing and relay latency on the targeted node. Repeated connections amplify the effect. The node's ability to participate in block propagation is impaired, which can cause it to fall behind the chain tip.

### Likelihood Explanation

TCP RPC is opt-in (`tcp_listen_address` is commented out by default) and defaults to `127.0.0.1:18114`. [8](#0-7) 

Operators who enable it for subscription use (its documented purpose) and expose it on a non-loopback interface are vulnerable. Mining pools and infrastructure operators commonly do this. No authentication is required once the port is reachable. The exploit requires only a TCP connection and a crafted JSON payload — no keys, no PoW, no privileged role.

### Recommendation

Move the batch-limit check out of `handle_jsonrpc` and into a shared middleware layer (or into the `MetaIoHandler` dispatch path) so it applies uniformly to HTTP, WS, and TCP transports. Alternatively, wrap the TCP `serve_stream_sink` call with a pre-processing step that deserializes the incoming line, enforces `JSONRPC_BATCH_LIMIT`, and rejects oversized batches before dispatching to the handler.

### Proof of Concept

```python
import socket, json

calls = [
    {"jsonrpc": "2.0", "method": "get_fee_rate_statistics", "params": [], "id": i}
    for i in range(200)
]
payload = json.dumps(calls) + "\n"

s = socket.create_connection(("127.0.0.1", 18114))
s.sendall(payload.encode())
# Node RocksDB IOPS spike: 200 * 202 = 40,400 reads from one frame.
# Repeat in a loop to exhaust I/O budget and degrade block processing latency.
```

### Citations

**File:** rpc/src/server.rs (L165-165)
```rust
            let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```

**File:** rpc/src/server.rs (L166-171)
```rust
            let stream_config = StreamServerConfig::default()
                .with_channel_size(4)
                .with_pipeline_size(4)
                .with_shutdown(async move {
                    new_tokio_exit_rx().cancelled().await;
                });
```

**File:** rpc/src/server.rs (L190-191)
```rust
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
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

**File:** rpc/src/util/fee_rate.rs (L39-47)
```rust
        let tip_number = self.get_tip_number();
        let start = std::cmp::max(
            MIN_TARGET,
            tip_number.saturating_add(1).saturating_sub(target),
        );

        let block_ext_iter =
            (start..=tip_number).filter_map(|number| self.get_block_ext_by_number(number));
        block_ext_iter.fold(Vec::new(), f)
```

**File:** rpc/src/util/fee_rate.rs (L56-59)
```rust
    fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
        self.get_block_hash(number)
            .and_then(|hash| self.get_block_ext(&hash))
    }
```

**File:** resource/ckb.toml (L195-197)
```text
# By default RPC only binds to HTTP service, you can bind it to TCP and WebSocket.
# tcp_listen_address = "127.0.0.1:18114"
# ws_listen_address = "127.0.0.1:28114"
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
