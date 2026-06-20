### Title
Unbounded TCP Connection Acceptance in `start_tcp_server` Enables File-Descriptor and Tokio-Task Exhaustion DoS — (`rpc/src/server.rs`)

### Summary

`start_tcp_server` accepts every inbound TCP connection unconditionally, spawning an unbounded `tokio::spawn` task per connection with no concurrent-connection cap, no per-IP rate limit, and no idle-connection timeout. An unprivileged attacker who can reach the `tcp_listen_address` port can open tens of thousands of connections, exhaust OS file descriptors and tokio task memory, and render the node unable to process P2P messages or serve any RPC.

---

### Finding Description

In `rpc/src/server.rs`, `start_tcp_server` runs the following accept loop:

```rust
while let Ok((stream, _)) = listener.accept().await {
    // ...
    tokio::spawn(async move {
        // serve_stream_sink holds the connection open indefinitely
        // waiting for newline-delimited JSON
    });
}
``` [1](#0-0) 

Every accepted connection immediately spawns a new tokio task. There is no:
- maximum concurrent connection counter
- per-source-IP connection limit
- idle-connection timeout (contrast: the HTTP server applies a 30-second `TimeoutLayer`) [2](#0-1) 

The `StreamServerConfig` for the TCP path sets only `channel_size(4)` and `pipeline_size(4)` — these bound per-connection internal channel depth, not the number of connections. [3](#0-2) 

Each spawned task holds an open `TcpStream` (one OS file descriptor) and a live tokio task allocation. A client that connects but never sends data keeps the task alive indefinitely because `serve_stream_sink` blocks waiting for a newline-delimited JSON frame.

The TCP RPC server is activated when the operator sets `rpc.tcp_listen_address` in the node config — a documented, supported production option. [4](#0-3) 

By contrast, the P2P network layer explicitly caps connections at 1024: [5](#0-4) 

No equivalent guard exists for the TCP RPC path.

---

### Impact Explanation

- **File descriptor exhaustion**: Linux default soft FD limit is 1024 per process (hard limit typically 65536). Opening ~1024–65536 idle TCP connections consumes all available FDs; subsequent `accept()` calls fail, and the node cannot accept new P2P connections or open outbound connections.
- **Tokio task memory pressure**: Each task allocates stack and heap. 10,000+ tasks cause significant memory pressure, degrading scheduler latency for all other async work including P2P message processing, block relay, and HTTP RPC.
- **No self-recovery**: Because there is no idle timeout, connections held open by the attacker persist until the attacker closes them or the node is restarted.

Scoped impact: **whole-node denial of service** — P2P connectivity degrades and all RPC endpoints become unresponsive.

---

### Likelihood Explanation

The TCP RPC port is opt-in but is a documented production feature used for subscription clients (e.g., `telnet localhost 18114`). Operators who expose it on a non-loopback interface (e.g., `0.0.0.0:18114`) are following a supported configuration path. No authentication, TLS, or firewall is enforced by the node itself. The attack requires only the ability to open TCP connections — no credentials, no PoW, no protocol knowledge.

---

### Recommendation

1. **Add a concurrent-connection semaphore** — use `tokio::sync::Semaphore` with a configurable limit (e.g., 100–500) before calling `tokio::spawn`; reject or drop connections that exceed the limit.
2. **Add an idle-connection timeout** — wrap the `serve_stream_sink` future with `tokio::time::timeout` so connections that send no data within N seconds are dropped.
3. **Add per-IP connection counting** — track active connections per source IP and reject new ones above a threshold.
4. **Document firewall requirements** — make clear in the config that `tcp_listen_address` must not be exposed to untrusted networks without external rate-limiting.

---

### Proof of Concept

```bash
# Open 10000 idle TCP connections to the TCP RPC port
python3 -c "
import socket, time
socks = []
for i in range(10000):
    try:
        s = socket.socket()
        s.connect(('127.0.0.1', 18114))
        socks.append(s)
    except Exception as e:
        print(f'Failed at {i}: {e}')
        break
print(f'Opened {len(socks)} connections')
time.sleep(300)  # hold open
"
# In another terminal, verify node P2P connectivity has degraded:
# - ckb RPC get_peers returns empty or errors
# - new TCP connections to the RPC port fail with EMFILE / connection refused
```

Each connection spawns one tokio task (visible via `/proc/<pid>/status` `Threads` or tokio metrics). After FD exhaustion, `listener.accept()` returns errors and the node cannot establish new P2P sessions.

### Citations

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
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

**File:** rpc/src/server.rs (L176-193)
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
```

**File:** util/app-config/src/configs/rpc.rs (L29-33)
```rust
    /// RPC TCP server listen addresses.
    ///
    /// Only TCP and WS are supported to subscribe events via the Subscription RPC module.
    #[serde(default)]
    pub tcp_listen_address: Option<String>,
```

**File:** network/src/network.rs (L976-976)
```rust
            .max_connection_number(1024)
```
