All technical claims verified against the actual code. Here is the validation result:

---

Audit Report

## Title
TCP RPC Server Bypasses `JSONRPC_BATCH_LIMIT` Entirely, Allowing Unbounded Batch Execution — (`rpc/src/server.rs`)

## Summary
`JSONRPC_BATCH_LIMIT` is enforced only inside `handle_jsonrpc`, the axum HTTP POST handler. The TCP server path in `start_tcp_server` calls `serve_stream_sink` directly with the raw `MetaIoHandler`, with no interposed batch-size check. An attacker with TCP access can send a single newline-terminated 2 MB JSON batch containing thousands of calls, triggering tens of thousands of synchronous RocksDB reads and degrading block processing and relay latency on the targeted node.

## Finding Description
`JSONRPC_BATCH_LIMIT` is a process-global `OnceLock<usize>` initialized from `config.rpc_batch_limit`. [1](#0-0) 

The batch-size guard is checked only inside `handle_jsonrpc`, the axum HTTP POST handler, under the `Request::Batch` arm: [2](#0-1) 

`start_tcp_server` never calls `handle_jsonrpc`. It calls `serve_stream_sink` directly with the raw `MetaIoHandler`, with no pre-processing step that checks `JSONRPC_BATCH_LIMIT`: [3](#0-2) 

The `LinesCodec` frame limit is 2 MB, meaning a single newline-delimited TCP message can carry a very large JSON batch array: [4](#0-3) 

`pipeline_size=4` limits concurrent *messages* (lines), not calls within a single batch message. One 2 MB line is one message, so all its batch calls execute together inside that single dispatch: [5](#0-4) 

Each `get_fee_rate_statistics` call invokes `FeeRateCollector::statistics`, which iterates up to `MAX_TARGET = 101` blocks, calling `get_block_hash` + `get_block_ext` per block — up to 202 synchronous RocksDB reads per call: [6](#0-5) [7](#0-6) 

The WebSocket path also bypasses `handle_jsonrpc`: `start_server` routes WebSocket connections to `handle_jsonrpc_ws` from the `jsonrpc_utils` crate, which is a separate handler that never invokes the batch-limit check: [8](#0-7) 

The existing test `test_rpc_batch_request_limit` uses `rpc_batch`, which calls `send_request` via `self.rpc_client.post(&self.rpc_uri)` — HTTP only. No test exercises the TCP batch limit path: [9](#0-8) [10](#0-9) 

## Impact Explanation
A single TCP connection sending one crafted 2 MB JSON batch saturates RocksDB I/O and the Tokio thread pool with tens of thousands of synchronous DB reads. This degrades block processing and relay latency on the targeted node. Repeated connections amplify the effect. For mining pool or infrastructure nodes that have enabled TCP RPC and exposed it on a non-loopback interface, this impairs their ability to participate in block propagation, which can cause the node to fall behind the chain tip. This matches the allowed impact: **High — Vulnerabilities or bad designs which could cause CKB network congestion with few costs**, since degrading block-propagating nodes with minimal attacker cost (one TCP connection, one crafted payload) directly impairs network health.

## Likelihood Explanation
TCP RPC is opt-in (`tcp_listen_address` is commented out by default) and defaults to `127.0.0.1:18114`: [11](#0-10) 

`rpc_batch_limit` is also `None` by default (commented out), meaning even operators who set it receive no protection on the TCP path: [12](#0-11) 

Operators who enable TCP RPC for subscription use (its documented purpose) and expose it on a non-loopback interface — a common pattern for mining pools and infrastructure operators — are vulnerable. No authentication is required once the port is reachable. The exploit requires only a TCP connection and a crafted JSON payload; no keys, no PoW, no privileged role.

## Recommendation
Move the batch-limit check out of `handle_jsonrpc` and into a shared layer applied uniformly to all transports. Concretely, wrap the TCP `serve_stream_sink` call with a pre-processing step that deserializes the incoming line, checks `JSONRPC_BATCH_LIMIT`, and rejects oversized batches before dispatching to `MetaIoHandler`. The same gap exists for the WebSocket path (`handle_jsonrpc_ws` from `jsonrpc_utils` also bypasses `handle_jsonrpc`), so both should be addressed together.

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
# Triggers 200 * 202 = 40,400 synchronous RocksDB reads from one TCP frame.
# Repeat in a tight loop to exhaust I/O budget and degrade block processing latency.
# Even with rpc_batch_limit = 200 configured, the TCP path ignores it entirely.
```
To confirm the bypass: set `rpc_batch_limit = 5` in `ckb.toml`, enable `tcp_listen_address`, send a 10-call batch over HTTP (rejected) and the same batch over TCP (accepted and executed). The existing test suite has no TCP batch limit coverage, confirming the gap.

### Citations

**File:** rpc/src/server.rs (L34-54)
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
```

**File:** rpc/src/server.rs (L112-114)
```rust
        let get_router = if enable_websocket {
            get(handle_jsonrpc_ws::<Option<Session>>)
        } else {
```

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

**File:** rpc/src/server.rs (L188-192)
```rust
                                });
                                tokio::pin!(w);
                                if let Err(err) = serve_stream_sink(&rpc, w, r, stream_config).await {
                                    info!("TCP RPCServer error: {:?}", err);
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

**File:** rpc/src/util/fee_rate.rs (L8-8)
```rust
const MAX_TARGET: u64 = 101;
```

**File:** rpc/src/util/fee_rate.rs (L56-59)
```rust
    fn get_block_ext_by_number(&self, number: BlockNumber) -> Option<BlockExt> {
        self.get_block_hash(number)
            .and_then(|hash| self.get_block_ext(&hash))
    }
```

**File:** rpc/src/tests/mod.rs (L91-95)
```rust
    fn rpc_batch(&self, request: &[RpcTestRequest]) -> Result<Vec<RpcTestResponse>, String> {
        let res = self.send_request(request);
        res.json::<Vec<RpcTestResponse>>()
            .map_err(|res| format!("batch request failed : {:?}", res))
    }
```

**File:** rpc/src/tests/module/test.rs (L108-132)
```rust
#[test]
fn test_rpc_batch_request_limit() {
    let suite = setup_rpc();
    let single_request = RpcTestRequest {
        id: 42,
        jsonrpc: "2.0".to_string(),
        method: "generate_epochs".to_string(),
        params: vec!["0x20000000000".into()],
    };

    let mut batch_request = vec![];
    for _i in 0..1001 {
        batch_request.push(single_request.clone());
    }

    // exceed limit with 1001
    let res = suite.rpc_batch(&batch_request);
    assert!(res.is_err());
    eprintln!("res: {:?}", res);

    // batch request will success with 1000
    batch_request.remove(0);
    let res = suite.rpc_batch(&batch_request);
    assert!(res.is_ok());
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
