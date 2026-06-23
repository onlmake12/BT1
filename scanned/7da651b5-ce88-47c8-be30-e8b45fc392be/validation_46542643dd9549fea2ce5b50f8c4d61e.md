### Title
Unbounded TCP Connection Acceptance in `start_tcp_server` Enables FD Exhaustion DoS — (`rpc/src/server.rs`)

### Summary

`start_tcp_server` accepts an unlimited number of TCP connections, spawning one unbounded `tokio::spawn` task per connection with no idle timeout and no connection cap. An attacker who can reach the TCP RPC port can open thousands of idle connections, exhaust the process's file-descriptor limit, and permanently halt the TCP RPC accept loop — and, if the process-wide FD limit is hit, also the HTTP RPC server.

### Finding Description

The accept loop in `start_tcp_server` has three compounding defects:

**1. No connection limit.** [1](#0-0) 

Every `accept()` success immediately spawns a new Tokio task. There is no semaphore, counter, or backpressure mechanism.

**2. No idle timeout.** [2](#0-1) 

Each spawned task calls `serve_stream_sink`, which blocks on `FramedRead` waiting for a newline-terminated JSON line. A client that connects and sends nothing holds the task — and its file descriptor — indefinitely.

**3. Accept loop exits permanently on error.** [3](#0-2) 

`while let Ok((stream, _)) = listener.accept().await` breaks out of the loop on any `Err`, including `EMFILE` (too many open files). Once the FD limit is hit, the TCP RPC server stops accepting connections entirely and never recovers without a process restart.

Contrast with the HTTP/WS path, which uses `axum::serve` (a maintained framework with its own connection management), and the P2P layer, which enforces `.max_connection_number(1024)`: [4](#0-3) 

The TCP RPC server has no equivalent guard.

**Configuration context:** TCP RPC is opt-in and defaults to `127.0.0.1:18114` when enabled. [5](#0-4) 

When enabled, the attacker only needs local access (or network access if the operator binds to `0.0.0.0`).

### Impact Explanation

- The TCP RPC accept loop exits permanently after FD exhaustion; all subsequent legitimate TCP RPC calls fail with connection refused.
- If the process-wide FD limit is reached, the HTTP RPC server (`axum::serve`) also fails to accept new connections, taking down the entire local RPC API.
- The node process itself does not crash, but the RPC surface becomes unavailable until the process is restarted.

This matches the stated scope: **"Any local RPC API crash" (0–500 points)**.

### Likelihood Explanation

- Requires TCP RPC to be enabled (opt-in, but a documented production feature).
- Requires the attacker to reach the TCP RPC port — trivially satisfied for any local process, or remotely if the operator binds to a non-loopback address.
- The default Linux per-process FD limit is 1024 (`ulimit -n`); opening 1024 idle TCP connections is trivial with a simple script.
- No authentication, no rate limiting, no connection cap stands in the way.

### Recommendation

1. Add a maximum concurrent connection semaphore before spawning:
   ```rust
   let sem = Arc::new(tokio::sync::Semaphore::new(MAX_TCP_RPC_CONNECTIONS));
   // acquire permit before tokio::spawn; drop permit when task ends
   ```
2. Wrap `serve_stream_sink` with `tokio::time::timeout` to close idle connections.
3. Change the accept loop to handle transient errors (e.g., `EMFILE`) with a brief sleep and retry rather than breaking out permanently.

### Proof of Concept

```python
import socket, time

socks = []
for i in range(2000):
    try:
        s = socket.socket()
        s.connect(("127.0.0.1", 18114))
        socks.append(s)
    except Exception as e:
        print(f"Failed at {i}: {e}")
        break

time.sleep(5)
# Now attempt a legitimate RPC call via TCP — connection will be refused
import subprocess
result = subprocess.run(
    ["nc", "127.0.0.1", "18114"],
    input=b'{"id":1,"jsonrpc":"2.0","method":"get_tip_block_number","params":[]}\n',
    capture_output=True, timeout=3
)
print("RPC response:", result.stdout)  # Expected: empty / connection refused
```

After the FD limit is hit, `listener.accept()` returns `Err(EMFILE)`, the `while let Ok(...)` loop at line 176 exits, and no further TCP RPC connections are accepted. [6](#0-5)

### Citations

**File:** rpc/src/server.rs (L174-195)
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
                    } => {},
```

**File:** network/src/network.rs (L976-976)
```rust
            .max_connection_number(1024)
```

**File:** resource/ckb.toml (L195-197)
```text
# By default RPC only binds to HTTP service, you can bind it to TCP and WebSocket.
# tcp_listen_address = "127.0.0.1:18114"
# ws_listen_address = "127.0.0.1:28114"
```
