### Title
Unbounded TCP RPC Connection Acceptance with No Rate Limiting or Connection Cap - (File: `rpc/src/server.rs`)

---

### Summary

The CKB node's TCP RPC server (`start_tcp_server`) accepts an unlimited number of concurrent connections, spawning a new async task per connection with no connection count tracking, per-IP throttling, or global cap. Additionally, the JSON-RPC batch request size limit (`rpc_batch_limit`) is disabled by default, allowing any RPC caller to submit arbitrarily large batch requests that consume unbounded CPU and memory.

---

### Finding Description

**Root Cause 1 — TCP RPC server: unbounded connection acceptance**

In `start_tcp_server`, every accepted TCP connection immediately spawns a new `tokio::spawn` task with no limit:

```rust
while let Ok((stream, _)) = listener.accept().await {
    let rpc = Arc::clone(&rpc);
    let stream_config = stream_config.clone();
    let codec = codec.clone();
    tokio::spawn(async move {   // ← new task per connection, no cap
        ...
        serve_stream_sink(&rpc, w, r, stream_config).await
    });
}
```

There is no counter, semaphore, or connection limit of any kind. The `RpcConfig` struct has no field for a maximum TCP connection count. [1](#0-0) [2](#0-1) 

**Root Cause 2 — Batch request limit disabled by default**

The `JSONRPC_BATCH_LIMIT` static is only populated when `config.rpc_batch_limit` is `Some(...)`. The production default config leaves this commented out:

```toml
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow
# rpc_batch_limit = 2000
```

When `rpc_batch_limit` is `None` (the default), the guard `if let Some(batch_size) = JSONRPC_BATCH_LIMIT.get()` is never true, so a caller can submit a batch of arbitrary size and all calls are processed sequentially. [3](#0-2) [4](#0-3) [5](#0-4) 

---

### Impact Explanation

**TCP connection flood**: An attacker with access to the TCP RPC port can open tens of thousands of persistent connections. Each connection allocates a `tokio` task, a `LinesCodec` buffer (up to 2 MiB per connection), and associated OS file descriptors. This exhausts memory, file descriptor limits, and Tokio worker threads, making the node unresponsive to legitimate RPC calls, block template requests, and transaction submissions. Block production can stall if the miner's `get_block_template` calls time out. [6](#0-5) 

**Batch request amplification**: Without a batch limit, a single HTTP POST containing thousands of expensive calls (e.g., `get_block_template`, `get_transaction_proof`, `verify_transaction_proof`) forces the RPC thread pool to process all of them, starving other callers and consuming CPU proportional to batch size. [7](#0-6) 

---

### Likelihood Explanation

- The TCP RPC server is optional (`tcp_listen_address` is disabled by default), but when enabled it defaults to `127.0.0.1:18114`. Any local process — including a malicious script or compromised local service — can exploit this without any credentials.
- The HTTP RPC (`127.0.0.1:8114`) is always enabled and the batch limit is off by default. Any local RPC caller (the scope explicitly includes "RPC caller" and "supported local CLI/RPC user") can trigger the batch amplification path.
- The config comment itself acknowledges the risk: *"a huge batch request may cost a lot of memory or makes the RPC server slow"*, confirming the developers are aware of the gap but left it opt-in.

**Impact: 3 | Likelihood: 3**

---

### Recommendation

1. **TCP RPC server**: Introduce a configurable maximum concurrent connection count (e.g., `rpc_tcp_max_connections`). Use a `tokio::sync::Semaphore` or an atomic counter to reject connections beyond the limit.
2. **Batch limit**: Change `rpc_batch_limit` to have a safe non-`None` default (e.g., 100 or 1000) rather than being opt-in. Operators who need larger batches can raise it explicitly.
3. Notify callers when limits are exceeded with a structured JSON-RPC error response.

---

### Proof of Concept

**TCP connection flood (bash):**
```bash
# Open 10,000 persistent TCP connections to the RPC TCP port
for i in $(seq 1 10000); do
  nc -q 0 127.0.0.1 18114 &
done
# Node's file descriptor limit is exhausted; subsequent legitimate RPC calls fail
```

**Batch amplification (curl, default config with no rpc_batch_limit):**
```bash
# Build a batch of 50,000 get_block_template calls
python3 -c "
import json, sys
batch = [{'id': i, 'jsonrpc': '2.0', 'method': 'get_block_template', 'params': [None, None, None]} for i in range(50000)]
print(json.dumps(batch))
" > big_batch.json

curl -s -X POST http://127.0.0.1:8114/ \
  -H 'Content-Type: application/json' \
  -d @big_batch.json
# Node processes all 50,000 calls sequentially, blocking the RPC thread pool
```

The TCP server loop at `rpc/src/server.rs:176` spawns one task per connection with no cap, and the batch handler at `rpc/src/server.rs:274-282` only enforces a limit when `JSONRPC_BATCH_LIMIT` is set — which it is not by default. [8](#0-7) [9](#0-8)

### Citations

**File:** rpc/src/server.rs (L53-55)
```rust
        if let Some(jsonrpc_batch_limit) = config.rpc_batch_limit {
            let _ = JSONRPC_BATCH_LIMIT.get_or_init(|| jsonrpc_batch_limit);
        }
```

**File:** rpc/src/server.rs (L162-202)
```rust
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

**File:** util/app-config/src/configs/rpc.rs (L26-61)
```rust
pub struct Config {
    /// RPC server listen addresses.
    pub listen_address: String,
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
    /// RPC WS server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub ws_listen_address: Option<String>,
    /// Max request body size in bytes.
    pub max_request_body_size: usize,
    /// Number of RPC worker threads.
    pub threads: Option<usize>,
    /// Number of RPC batch limit.
    pub rpc_batch_limit: Option<usize>,
    /// Enabled RPC modules.
    pub modules: Vec<Module>,
    /// Rejects txs with scripts that might trigger known bugs
    #[serde(default)]
    pub reject_ill_transactions: bool,
    /// Whether enable deprecated RPC methods.
    ///
    /// Deprecated RPC methods are disabled by default.
    #[serde(default)]
    pub enable_deprecated_rpc: bool,
    /// Customized extra well known lock scripts.
    #[serde(default)]
    pub extra_well_known_lock_scripts: Vec<Script>,
    /// Customized extra well known type scripts.
    #[serde(default)]
    pub extra_well_known_type_scripts: Vec<Script>,
}
```

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
```
